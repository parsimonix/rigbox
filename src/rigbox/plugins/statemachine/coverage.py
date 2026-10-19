"""Track state-machine states, transitions, and guard outcomes observed during runs.

Coverage items use readable identifiers so saved data survives machine edits. Items no longer present in the
machine are ignored when summaries are built.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from rigbox.core.registry import CommandError
from rigbox.plugins.statemachine.schema import MachineSpec, Transition


def transition_key(src: str, t: Transition) -> str:
    """Build the stable identifier for transition `t` taken from `src`.

    Args:
        src: The transition's source state.
        t: The transition to identify.

    Returns:
        A readable key for the transition from that source.
    """
    guard = f"[{t.guard}]" if t.guard else ""
    return f"{src} --{t.event}{guard}--> {t.target}"


def guard_key(src: str, t: Transition, outcome: bool) -> str:
    """Build the stable identifier for one guard outcome.

    Args:
        src: The transition's source state.
        t: The guarded transition.
        outcome: Whether the guard evaluated to true.

    Returns:
        A readable key for this guard outcome.
    """
    return f"{transition_key(src, t)} guard={'true' if outcome else 'false'}"


class Coverage(BaseModel):
    """Track covered items across one or more runs of the same machine."""

    machine: str
    states: set[str] = Field(default_factory=set)
    transitions: set[str] = Field(default_factory=set)
    guards: set[str] = Field(default_factory=set)

    def merge(self, other: Coverage) -> None:
        """Add everything covered in `other` to this coverage.

        Args:
            other: Coverage collected from another run.

        Raises:
            CommandError: If `other` belongs to a different machine.
        """
        if other.machine != self.machine:
            raise CommandError(f"coverage is for machine '{other.machine}', not '{self.machine}'")
        self.states |= other.states
        self.transitions |= other.transitions
        self.guards |= other.guards

    def save(self, path: Path) -> None:
        """Write this coverage as JSON, creating parent directories as needed.

        Args:
            path: Destination file.

        Raises:
            OSError: If the destination cannot be created or written.
        """
        data = {"machine": self.machine, **{k: sorted(getattr(self, k)) for k in ("states", "transitions", "guards")}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Coverage:
        """Load and validate coverage from a JSON file.

        Args:
            path: Source file.

        Raises:
            CommandError: If the file can't be read or is invalid.

        Returns:
            The validated coverage data.
        """
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as e:
            raise CommandError(f"cannot read coverage file {path}: {e}") from None


def universe(spec: MachineSpec) -> dict[str, set[str]]:
    """List every state, transition, and guard outcome that could be covered.

    Args:
        spec: The machine to inspect.

    Returns:
        All coverable items, grouped by kind.
    """
    transitions: set[str] = set()
    guards: set[str] = set()
    for t in spec.transitions:
        for src in spec.sources(t):
            transitions.add(transition_key(src, t))
            if t.guard:
                guards |= {guard_key(src, t, True), guard_key(src, t, False)}
    return {"states": set(spec.state_names), "transitions": transitions, "guards": guards}


def summary(spec: MachineSpec, cov: Coverage) -> list[dict[str, Any]]:
    """Summarize coverage counts and uncovered items for each kind.

    Args:
        spec: The machine whose coverage is being reported.
        cov: The accumulated coverage to summarize.

    Returns:
        One row per coverage kind with counts, percentage, and uncovered items.
    """
    rows: list[dict[str, Any]] = []
    for kind, total in universe(spec).items():
        covered = total & getattr(cov, kind)
        rows.append(
            {
                "kind": kind,
                "covered": len(covered),
                "total": len(total),
                "percent": round(100 * len(covered) / len(total), 1) if total else 100.0,
                "uncovered": sorted(total - covered),
            }
        )
    return rows


def describe(spec: MachineSpec, cov: Coverage) -> str:
    """Format a one-line summary of covered item counts.

    Args:
        spec: The machine whose coverage is being reported.
        cov: The accumulated coverage to summarize.

    Returns:
        A compact text summary of coverage by kind.
    """
    return "coverage: " + ", ".join(f"{r['kind']} {r['covered']}/{r['total']}" for r in summary(spec, cov))
