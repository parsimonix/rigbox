"""Define and validate YAML state-machine manifests with Pydantic models.

The models check names and cross-references, while expression validators reject malformed guards and invariants
before a machine is simulated or replayed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rigbox.core.registry import CommandError

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _check_name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(f"'{value}' must start with a letter and use only letters, digits, underscore")
    return value


def _check_expression(value: str | None) -> str | None:
    if value is not None:
        try:
            ast.parse(value, mode="eval")
        except SyntaxError as e:
            raise ValueError(f"invalid expression '{value}': {e.msg}") from None
    return value


def _escape(text: str) -> str:
    return text.replace("<", "#lt;").replace(">", "#gt;")


class State(BaseModel):
    """Describe a machine state, including its safety and required-event declarations."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="The name of the state")
    description: str = Field("", description="A brief description of the state")
    safe: bool = Field(False, description="Whether or not the state the machine is allowed to settle in this state; used by the safe-path property check")
    required_events: list[str] = Field(
        default_factory=list,
        description="Events that must have a declared outgoing transition in this state; guards are not evaluated",
    )

    valid_name = field_validator("name")(_check_name)


class Event(BaseModel):
    """Describe an event and its optional decoded-packet matching expression."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="The name of the event")
    description: str = Field("", description="A brief description of the event")
    match: str | None = Field(None, description="Expression over a decoded packet's fields; used by trace replay")

    valid_name = field_validator("name")(_check_name)
    valid_match = field_validator("match")(_check_expression)


class Transition(BaseModel):
    """Describe an event-driven transition with optional source states and guard."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str | list[str] = Field(alias="from", description="State name, list of names, or '*' for any state")
    event: str
    target: str = Field(alias="to")
    guard: str | None = Field(None, description="Expression over the event's data; must be true to fire")
    actions: list[str] = Field(default_factory=list, description="Labels reported when the transition fires")
    on_enter: str | None = Field(None, description="Action to execute upon entering the target state, if any")

    valid_guard = field_validator("guard")(_check_expression)


class MachineSpec(BaseModel):
    """Define a complete machine, including its states, events, transitions, and invariants."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    initial: str
    state_field: str | None = Field(None, description="Packet field holding the device's own state name (replay)")
    states: list[State] = Field(min_length=1)
    events: list[Event] = Field(min_length=1)
    errors: list[str] = Field(default_factory=list, description="Names of errors reported by the state machine")
    transitions: list[Transition] = Field(default_factory=list)
    constants: dict[str, str] = Field(
        default_factory=dict,
        description="Key-value pairs of machine-specific constants",
    )
    invariants: list[str] = Field(
        default_factory=list,
        description="Must stay true after every fuzzed step. Names: state, prev_state, event, fired, plus event data",
    )

    @classmethod
    def load_from_file(cls, path: Path) -> Self:
        """Load and validate a machine manifest from a file.

        Args:
            path: The manifest file to load.

        Raises:
            CommandError: If the file can't be read or the manifest is invalid.

        Returns:
            The validated machine specification.
        """
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise CommandError(f"cannot read {path}: {e}") from None
        try:
            return MachineSpec.model_validate(data)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'machine'}: {err['msg']}" for err in e.errors())
            raise CommandError(f"{path.name}: {problems}") from None

    @field_validator("invariants")
    @classmethod
    def valid_invariants(cls, values: list[str]) -> list[str]:
        """Validate that every invariant parses as an expression.

        Args:
            values: The invariant expressions to validate.

        Raises:
            ValueError: If an invariant is not a valid expression.

        Returns:
            The validated invariant expressions.
        """
        for value in values:
            _check_expression(value)
        return values

    @model_validator(mode="after")
    def _references(self) -> Self:
        """Validate names and references across the machine specification.

        Raises:
            ValueError: If names are duplicated or a state, event, or transition reference is invalid.

        Returns:
            This validated machine specification.
        """
        states = [s.name for s in self.states]
        events = [e.name for e in self.events]
        for kind, names in (("state", states), ("event", events)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"duplicate {kind} names: {', '.join(dupes)}")
        if self.initial not in states:
            raise ValueError(f"initial state '{self.initial}' is not defined")
        for state in self.states:
            if len(state.required_events) != len(set(state.required_events)):
                raise ValueError(f"duplicate required events in state '{state.name}'")
            for event in state.required_events:
                if event not in events:
                    raise ValueError(f"state '{state.name}' requires unknown event '{event}'")
        for t in self.transitions:
            for ref in self.sources(t) if t.source != "*" else []:
                if ref not in states:
                    raise ValueError(f"transition from unknown state '{ref}'")
            if t.target not in states:
                raise ValueError(f"transition to unknown state '{t.target}'")
            if t.event not in events:
                raise ValueError(f"transition uses unknown event '{t.event}'")
        return self

    def sources(self, t: Transition) -> list[str]:
        """Expand a transition's source into concrete state names.

        Args:
            t: The transition to expand; `*` expands to every state.

        Returns:
            The concrete source state names.
        """
        if t.source == "*":
            return [s.name for s in self.states]
        return [t.source] if isinstance(t.source, str) else list(t.source)

    @property
    def state_names(self) -> list[str]:
        """Return all state names in declaration order.

        Returns:
            The declared state names.
        """
        return [s.name for s in self.states]

    @property
    def max_actions(self) -> int:
        """Return the maximum number of actions in any transition.

        Returns:
            The maximum number of actions.
        """
        return max((len(t.actions) for t in self.transitions), default=0)

    def to_mermaid(self) -> str:
        """Render the machine as Mermaid `stateDiagram-v2` text.

        Returns:
            The Mermaid source for this machine.
        """
        lines = ["stateDiagram-v2", f"\t[*] --> {self.initial}"]
        for s in self.states:
            if s.description:
                lines.append(f"\t{s.name} : {_escape(s.description)}")
        for t in self.transitions:
            label = t.event
            if t.guard:
                label += f" [{_escape(t.guard)}]"
            if t.actions:
                label += " / " + ", ".join(t.actions)
            for src in self.sources(t):
                lines.append(f"\t{src} --> {t.target} : {label}")
        safe = [s.name for s in self.states if s.safe]
        if safe:
            lines.append("\tclassDef safe fill:#d4f4d4,stroke:#2e7d32")
            lines.append(f"\tclass {','.join(safe)} safe")
        return "\n".join(lines) + "\n"


def to_mermaid(spec: MachineSpec) -> str:
    """Render the the given `spec` as mermaid text.

    Args:
        spec (MachineSpec): The machine specification to render.

    Returns:
        str: The mermaid source.
    """
    return MachineSpec.to_mermaid(spec)
