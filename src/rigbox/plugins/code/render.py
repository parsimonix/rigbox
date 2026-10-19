"""Build a stable, target-specific context for C state-machine templates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rigbox.core.registry import CommandError
from rigbox.plugins.statemachine.schema import MachineSpec, Transition


@dataclass(frozen=True)
class CEnumMember:
    """Represent one named C enum constant and its assigned value."""

    symbol: str
    value: int


@dataclass(frozen=True)
class CStateMachineContext:
    """Expose only the C identifiers and declarations needed by the header template."""

    name: str
    version: str
    header_guard: str
    state_type: str
    event_type: str
    state_count_symbol: str
    event_count_symbol: str
    null_event_symbol: str
    max_actions: int
    states: tuple[CEnumMember, ...]
    events: tuple[CEnumMember, ...]
    transitions: tuple[Transition, ...]
    errors: tuple[str, ...]
    constants: dict[str, str]


def _identifier(value: str) -> str:
    """Normalize a value into a non-empty C identifier fragment.

    Args:
            value: The source name to normalize.

    Returns:
            An ASCII identifier fragment.

    Raises:
            CommandError: If the source name has no usable characters.
    """
    result = re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_")
    if not result:
        raise CommandError("machine name must contain an ASCII letter, digit, or underscore")
    if result[0].isdigit():
        result = f"_{result}"
    return result


def _enum_members(names: list[str], prefix: str, kind: str) -> tuple[CEnumMember, ...]:
    members = tuple(CEnumMember(f"{prefix}_{_identifier(name).upper()}", index) for index, name in enumerate(names))
    symbols = [member.symbol for member in members]
    if len(symbols) != len(set(symbols)):
        raise CommandError(f"{kind} names collide after conversion to C identifiers")
    return members


def c_state_machine_context(spec: MachineSpec) -> CStateMachineContext:
    """Build a C-facing context without exposing the full manifest model to templates.

    Args:
            spec: The validated state-machine specification.

    Returns:
            The header guard, enum types, and declaration-order state/event constants.

    Raises:
            CommandError: If the machine name is unusable or state/event names collide as C identifiers.
    """
    machine = _identifier(spec.name).lower()
    state_count_symbol = f"{machine}_STATE_NUM_STATES".upper()
    event_count_symbol = f"{machine}_EVENT_NUM_EVENTS".upper()
    null_event_symbol = f"{machine}_EVENT_NULL".upper()
    states = _enum_members(spec.state_names, f"{machine}_STATE", "state")
    events = _enum_members([event.name for event in spec.events], f"{machine}_EVENT", "event")
    if state_count_symbol in {member.symbol for member in states}:
        raise CommandError("a state name collides with the generated state-count symbol")
    reserved_event_symbols = {event_count_symbol, null_event_symbol}
    if reserved_event_symbols.intersection(member.symbol for member in events):
        raise CommandError("an event name collides with a generated event symbol")
    return CStateMachineContext(
        name=spec.name,
        version=spec.version,
        header_guard=f"{machine}_STATE_MACHINE_H".upper(),
        state_type=f"{machine}_state_t",
        event_type=f"{machine}_event_t",
        state_count_symbol=state_count_symbol,
        event_count_symbol=event_count_symbol,
        null_event_symbol=null_event_symbol,
        states=states,
        events=events,
        errors=tuple(spec.errors),
        max_actions=spec.max_actions,
        transitions=tuple(spec.transitions),
        constants={f"{machine}_{key}".upper(): value for key, value in spec.constants.items()},
    )
