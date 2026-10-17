"""Seeded random-walk fuzzing: random events with boundary-value data, checking invariants after each step."""

from __future__ import annotations

import ast
import random
from dataclasses import dataclass
from typing import Any

import yaml

from rigbox.core.registry import CommandError
from rigbox.plugins.manifests.schema import INT_RANGES, StructManifest
from rigbox.plugins.statemachine.coverage import Coverage
from rigbox.plugins.statemachine.engine import Simulator, evaluate
from rigbox.plugins.statemachine.schema import MachineSpec

# Names an invariant can use besides event data; never generated as data.
RESERVED = {"state", "prev_state", "event", "fired"}


@dataclass
class Failure:
    """A fuzz run that broke an invariant or hit an error, with the events that led to it."""

    run: int
    kind: str  # "error" | "invariant"
    detail: str
    steps: list[tuple[str, dict[str, Any]]]


def _around(value: object) -> list[Any]:
    if isinstance(value, bool):
        return [value, not value]
    if isinstance(value, (int, float)):
        return [value - 1, value, value + 1]
    return [value] if isinstance(value, str) else []


def value_pools(spec: MachineSpec, packet: StructManifest | None = None) -> dict[str, list[Any]]:
    """Return candidate values per variable used in the machine's expressions.

    Values are boundaries around the constants each variable is compared with, narrowed to the type ranges of
    `packet` when a packet manifest is given.
    """
    exprs = [t.guard for t in spec.transitions if t.guard] + list(spec.invariants)
    exprs += [e.match for e in spec.events if e.match]
    trees = [ast.parse(x, mode="eval") for x in exprs]
    names = sorted({n.id for tree in trees for n in ast.walk(tree) if isinstance(n, ast.Name)} - RESERVED)
    pools: dict[str, list[Any]] = {n: [] for n in names}
    for tree in trees:
        for cmp_ in (n for n in ast.walk(tree) if isinstance(n, ast.Compare)):
            operands = [cmp_.left, *cmp_.comparators]
            for var in operands:
                if isinstance(var, ast.Name) and var.id in pools:
                    for other in operands:
                        if isinstance(other, ast.Constant):
                            pools[var.id] += _around(other.value)
    for name, pool in pools.items():
        numeric = any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pool)
        pools[name] = list(dict.fromkeys(pool + ([0] if numeric else [] if pool else [True, False])))

    for f in packet.fields if packet else []:
        if f.name not in pools:
            continue
        numbers = [v for v in pools[f.name] if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if f.type in INT_RANGES:
            lo, hi = INT_RANGES[f.type]
            pools[f.name] = list(dict.fromkeys([min(max(int(round(v)), lo), hi) for v in numbers] + [lo, hi]))
        elif f.type == "bool":
            pools[f.name] = [True, False]
        elif f.type == "string":
            pools[f.name] = list(dict.fromkeys(pools[f.name] + [""]))
        else:  # floats keep their boundaries
            pools[f.name] = list(dict.fromkeys(pools[f.name] + [0.0]))
    return pools


def format_steps(steps: list[tuple[str, dict[str, Any]]]) -> str:
    """Steps in the form `sm.simulate --steps` accepts."""
    items = [{name: data} if data else name for name, data in steps]
    return yaml.safe_dump(items, default_flow_style=True, width=10**6).strip()


def _failure_of(sim: Simulator, spec: MachineSpec, name: str, data: dict[str, Any]) -> tuple[str, str] | None:
    prev = sim.state
    try:
        step = sim.send(name, data)
        for expr in spec.invariants:
            names = {**data, "state": sim.state, "prev_state": prev, "event": name, "fired": step.fired}
            if not evaluate(expr, names):
                return "invariant", f"invariant violated: {expr}"
    except CommandError as e:
        return "error", str(e)
    return None


def fuzz(
    spec: MachineSpec,
    *,
    runs: int,
    length: int,
    seed: int,
    packet: StructManifest | None = None,
    coverage: Coverage | None = None,
) -> list[Failure]:
    """Run `runs` random walks of up to `length` events each. A walk stops at its first failure."""
    rng = random.Random(seed)
    pools = value_pools(spec, packet)
    events = [e.name for e in spec.events]
    failures: list[Failure] = []
    for run in range(1, runs + 1):
        sim = Simulator(spec, coverage)
        steps: list[tuple[str, dict[str, Any]]] = []
        for _ in range(length):
            name = rng.choice(events)
            data = {var: rng.choice(pool) for var, pool in pools.items()}
            steps.append((name, data))
            found = _failure_of(sim, spec, name, data)
            if found:
                failures.append(Failure(run, found[0], found[1], steps))
                break
    return failures
