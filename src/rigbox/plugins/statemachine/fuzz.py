"""Fuzz state-machine models with seeded event sequences and boundary-focused data.

Each sequence runs through the simulator, which checks guards and invariants after every event. A fixed seed makes
failures reproducible.
"""

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
    """Record the first failure and reproducing event sequence from one fuzz run."""

    run: int
    kind: str  # "error" | "invariant"
    detail: str
    steps: list[tuple[str, dict[str, Any]]]


def _around(value: object) -> list[Any]:
    """Build candidate values around a constant used in an expression.

    Args:
        value: The comparison constant.

    Returns:
        Nearby numeric or boolean values, or the original string value.
    """
    if isinstance(value, bool):
        return [value, not value]
    if isinstance(value, (int, float)):
        return [value - 1, value, value + 1]
    return [value] if isinstance(value, str) else []


def value_pools(spec: MachineSpec, packet: StructManifest | None = None) -> dict[str, list[Any]]:
    """Build candidate values from expression boundaries and optional packet field types.

    Args:
        spec: The machine whose guards, matches, and invariants to inspect.
        packet: Optional packet manifest that constrains generated field values.

    Returns:
        Candidate values keyed by expression variable name.
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
    """Format events and data as the YAML list accepted by `sm.simulate`.

    Args:
        steps: Event names paired with their data.

    Returns:
        A compact YAML representation of the steps.
    """
    items = [{name: data} if data else name for name, data in steps]
    return yaml.safe_dump(items, default_flow_style=True, width=10**6).strip()


def _failure_of(sim: Simulator, spec: MachineSpec, name: str, data: dict[str, Any]) -> tuple[str, str] | None:
    """Run one event and check the machine's invariants afterward.

    Args:
        sim: The simulator to advance.
        spec: The machine whose invariants to check.
        name: The event to send.
        data: Values supplied with the event.

    Returns:
        The failure kind and detail, or `None` if the step succeeds.
    """
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
    """Run seeded random walks and collect the first failure from each run.

    Args:
        spec: The machine to fuzz.
        runs: Number of random walks to run.
        length: Maximum number of events in each walk.
        seed: Seed used to make the walks reproducible.
        packet: Optional packet manifest that constrains generated field values.
        coverage: Optional shared coverage to accumulate across runs.

    Returns:
        Failures found, each with the event sequence needed to reproduce it.
    """
    # Generate the random seed
    rng = random.Random(seed)
    # Prepare the value pools for generating random event data
    pools = value_pools(spec, packet)
    # Extract the event names from the machine specification
    events = [e.name for e in spec.events]
    # Initialize the list to collect failures
    failures: list[Failure] = []
    # Run each random walk
    for run in range(1, runs + 1):
        # Initialize the simulator for this run
        sim = Simulator(spec, coverage)
        # Initialize the list to record the steps taken in this run
        steps: list[tuple[str, dict[str, Any]]] = []
        # Perform the random walk for the specified length
        for _ in range(length):
            # Select a random event and generate its associated data
            name = rng.choice(events)
            data = {var: rng.choice(pool) for var, pool in pools.items()}
            # Record the step and check for failures
            steps.append((name, data))
            found = _failure_of(sim, spec, name, data)
            # If a failure is found, record it and stop the current run
            if found:
                failures.append(Failure(run, found[0], found[1], steps))
                break
        # End of the current run; proceed to the next run if any
    return failures
