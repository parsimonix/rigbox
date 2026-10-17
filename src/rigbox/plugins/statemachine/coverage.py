"""State machine coverage: states visited, transitions fired, and both outcomes of each guard.

Items are identified by readable strings (not indexes) so a coverage file survives edits to the machine;
items for things that no longer exist are simply ignored when reporting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from rigbox.core.registry import CommandError
from rigbox.plugins.statemachine.schema import MachineSpec, Transition


def transition_key(src: str, t: Transition) -> str:
    """Return the readable identifier of transition `t` when taken from state `src`."""
    guard = f"[{t.guard}]" if t.guard else ""
    return f"{src} --{t.event}{guard}--> {t.target}"


def guard_key(src: str, t: Transition, outcome: bool) -> str:
    """Return the identifier of one outcome (true/false) of the guard on `t` taken from `src`."""
    return f"{transition_key(src, t)} guard={'true' if outcome else 'false'}"


class Coverage(BaseModel):
    """What one or more runs of a machine have covered."""

    machine: str
    states: set[str] = Field(default_factory=set)
    transitions: set[str] = Field(default_factory=set)
    guards: set[str] = Field(default_factory=set)

    def merge(self, other: Coverage) -> None:
        """Add everything covered in `other` to this coverage.

        Raises:
            CommandError: If `other` belongs to a different machine.
        """
        if other.machine != self.machine:
            raise CommandError(f"coverage is for machine '{other.machine}', not '{self.machine}'")
        self.states |= other.states
        self.transitions |= other.transitions
        self.guards |= other.guards

    def save(self, path: Path) -> None:
        """Write this coverage to `path` as JSON, creating parent directories."""
        data = {"machine": self.machine, **{k: sorted(getattr(self, k)) for k in ("states", "transitions", "guards")}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Coverage:
        """Read coverage from the JSON file at `path`.

        Raises:
            CommandError: If the file can't be read or is invalid.
        """
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as e:
            raise CommandError(f"cannot read coverage file {path}: {e}") from None


def universe(spec: MachineSpec) -> dict[str, set[str]]:
    """Everything that could be covered."""
    transitions: set[str] = set()
    guards: set[str] = set()
    for t in spec.transitions:
        for src in spec.sources(t):
            transitions.add(transition_key(src, t))
            if t.guard:
                guards |= {guard_key(src, t, True), guard_key(src, t, False)}
    return {"states": set(spec.state_names), "transitions": transitions, "guards": guards}


def summary(spec: MachineSpec, cov: Coverage) -> list[dict[str, Any]]:
    """Return one row per coverage kind with covered/total counts, percentage and the uncovered items."""
    rows: list[dict[str, Any]] = []
    for kind, total in universe(spec).items():
        covered = total & getattr(cov, kind)
        rows.append({
            "kind": kind,
            "covered": len(covered),
            "total": len(total),
            "percent": round(100 * len(covered) / len(total), 1) if total else 100.0,
            "uncovered": sorted(total - covered),
        })
    return rows


def describe(spec: MachineSpec, cov: Coverage) -> str:
    """Return a one-line coverage summary."""
    return "coverage: " + ", ".join(f"{r['kind']} {r['covered']}/{r['total']}" for r in summary(spec, cov))
