"""Run a machine: `transitions` does the state handling, simpleeval evaluates guard/match expressions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from simpleeval import simple_eval
from transitions import Machine, MachineError

from rigbox.core.registry import CommandError
from rigbox.plugins.statemachine.coverage import Coverage, guard_key, transition_key
from rigbox.plugins.statemachine.schema import MachineSpec, Transition


def evaluate(expr: str, data: dict[str, Any]) -> bool:
    """Evaluate a guard/match expression against event data.

    Raises:
        CommandError: If the expression can't be evaluated.
    """
    try:
        return bool(simple_eval(expr, names=data))
    except Exception as e:  # simpleeval raises several types (unknown name, bad operand, ...)
        raise CommandError(f"cannot evaluate '{expr}': {e}") from None


def matches(expr: str, data: dict[str, Any]) -> bool:
    """Like `evaluate`, but an expression that can't be evaluated (e.g. missing field) simply doesn't match."""
    try:
        return evaluate(expr, data)
    except CommandError:
        return False


class Step(BaseModel):
    """The outcome of sending one event to the simulator."""

    event: str
    from_state: str
    to_state: str
    fired: bool
    reason: str = ""
    actions: list[str] = []


class _Model:
    """Holds the `state` attribute that transitions manages."""

    state: str
    trigger: Callable[..., bool]  # `transitions` adds a trigger() method to the model


class Simulator:
    """Runs one machine and tracks coverage; pass `coverage` to accumulate across several simulators."""

    def __init__(self, spec: MachineSpec, coverage: Coverage | None = None) -> None:
        """Build the underlying `transitions` machine for `spec`."""
        self.spec = spec
        self.coverage = coverage if coverage is not None else Coverage(machine=spec.name)
        self.coverage.states.add(spec.initial)
        self._events = {e.name for e in spec.events}
        self._fired: Transition | None = None
        self._before = spec.initial  # the state the current event was sent from
        self._model = _Model()
        self._machine = Machine(
            model=self._model,
            states=spec.state_names,
            initial=spec.initial,
            auto_transitions=False,
        )
        for t in spec.transitions:
            self._machine.add_transition(
                f"ev_{t.event}",  # prefixed so event names can never clash with attributes transitions adds
                spec.sources(t),  # ty: ignore[invalid-argument-type]  # valid; the stub's list type is invariant
                t.target,
                conditions=[self._guard(t)] if t.guard else None,
                after=self._recorder(t),
            )

    def _guard(self, t: Transition) -> Callable[[dict[str, Any] | None], bool]:
        expr = t.guard or ""  # only called for guarded transitions

        def guard(data: dict[str, Any] | None = None) -> bool:
            outcome = evaluate(expr, data or {})
            self.coverage.guards.add(guard_key(self._before, t, outcome))
            return outcome

        return guard

    def _recorder(self, t: Transition) -> Callable[[dict[str, Any] | None], None]:
        def record(data: dict[str, Any] | None = None) -> None:
            self._fired = t
            self.coverage.transitions.add(transition_key(self._before, t))

        return record

    @property
    def state(self) -> str:
        """The machine's current state name."""
        return self._model.state

    def set_state(self, name: str) -> None:
        """Force the machine into state `name` without firing a transition."""
        self._machine.set_state(name, model=self._model)
        self.coverage.states.add(name)

    def send(self, event: str, data: dict[str, Any] | None = None) -> Step:
        """Send `event` with optional `data` and report what happened.

        Raises:
            CommandError: If the event isn't defined or a guard can't be evaluated.
        """
        if event not in self._events:
            raise CommandError(f"unknown event: {event}")
        before = self._before = self.state
        self._fired = None
        try:
            moved = self._model.trigger(f"ev_{event}", data=data or {})
        except MachineError:
            reason = f"no transition from {before}"
            return Step(event=event, from_state=before, to_state=before, fired=False, reason=reason)
        if not moved:
            return Step(event=event, from_state=before, to_state=before, fired=False, reason="guard failed")
        self.coverage.states.add(self.state)
        actions: list[str] = (
            self._fired.actions if self._fired else []  # ty: ignore[redundant-condition]  # set by recorder callback
        )
        return Step(event=event, from_state=before, to_state=self.state, fired=True, actions=actions)
