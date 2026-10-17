"""The state machine manifest: states, events and guarded transitions, validated by pydantic."""

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


class State(BaseModel):
    """A named state of the machine."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    safe: bool = False  # a state the machine is allowed to rest in; used by the safe-path property check

    valid_name = field_validator("name")(_check_name)


class Event(BaseModel):
    """An event the machine reacts to, optionally recognised in decoded packets by ``match``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    match: str | None = Field(None, description="Expression over a decoded packet's fields; used by trace replay")

    valid_name = field_validator("name")(_check_name)
    valid_match = field_validator("match")(_check_expression)


class Transition(BaseModel):
    """A transition from one or more states to a target state on an event, with an optional guard."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str | list[str] = Field(alias="from", description="State name, list of names, or '*' for any state")
    event: str
    target: str = Field(alias="to")
    guard: str | None = Field(None, description="Expression over the event's data; must be true to fire")
    actions: list[str] = Field(default_factory=list, description="Labels reported when the transition fires")

    valid_guard = field_validator("guard")(_check_expression)


class MachineSpec(BaseModel):
    """A complete state machine definition."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    initial: str
    state_field: str | None = Field(None, description="Packet field holding the device's own state name (replay)")
    states: list[State] = Field(min_length=1)
    events: list[Event] = Field(min_length=1)
    transitions: list[Transition] = Field(default_factory=list)
    invariants: list[str] = Field(
        default_factory=list,
        description="Must stay true after every fuzzed step. Names: state, prev_state, event, fired, plus event data",
    )

    @field_validator("invariants")
    @classmethod
    def valid_invariants(cls, values: list[str]) -> list[str]:
        """Check that every invariant parses as an expression."""
        for value in values:
            _check_expression(value)
        return values

    @model_validator(mode="after")
    def _references(self) -> Self:
        states = [s.name for s in self.states]
        events = [e.name for e in self.events]
        for kind, names in (("state", states), ("event", events)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"duplicate {kind} names: {', '.join(dupes)}")
        if self.initial not in states:
            raise ValueError(f"initial state '{self.initial}' is not defined")
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
        """The concrete source states of a transition ('*' expands to every state)."""
        if t.source == "*":
            return [s.name for s in self.states]
        return [t.source] if isinstance(t.source, str) else list(t.source)

    @property
    def state_names(self) -> list[str]:
        """Names of all states, in declaration order."""
        return [s.name for s in self.states]


def load_machine(path: Path) -> MachineSpec:
    """Load and validate the machine file at `path`.

    Raises:
        CommandError: If the file can't be read or fails validation.
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
