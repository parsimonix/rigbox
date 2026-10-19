"""Simulate state-machine events and track their transition and guard outcomes.

The `transitions` library manages state changes, while `simpleeval` evaluates guard and match expressions against
event data.
"""

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

    Args:
        expr: The expression to evaluate.
        data: Names and values available to the expression.

    Returns:
        The expression's truth value.

    Raises:
        CommandError: If the expression can't be evaluated.
    """
    try:
        return bool(simple_eval(expr, names=data))
    except Exception as e:  # simpleeval raises several types (unknown name, bad operand, ...)
        raise CommandError(f"cannot evaluate '{expr}': {e}") from None


def matches(expr: str, data: dict[str, Any]) -> bool:
    """Treat an expression that cannot be evaluated as a non-match.

    Args:
        expr: The expression to evaluate.
        data: Names and values available to the expression.

    Returns:
        Whether the expression evaluates to true; evaluation errors return false.
    """
    try:
        return evaluate(expr, data)
    except CommandError:
        return False


class Step(BaseModel):
    """Describe the outcome of sending one event to the simulator."""

    event: str
    from_state: str
    to_state: str
    fired: bool
    reason: str = ""
    actions: list[str] = []


class _Model:
    """Provide the state and trigger attributes required by `transitions`."""

    state: str
    trigger: Callable[..., bool]  # `transitions` adds a trigger() method to the model


class Simulator:
    """Simulate machine transitions and track coverage across events."""

    def __init__(self, spec: MachineSpec, coverage: Coverage | None = None) -> None:
        """Build a simulator for the machine specification.

        Args:
            spec: The machine to simulate.
            coverage: Optional shared coverage to accumulate across simulators.
        """
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
        """Create a callback that evaluates and records this transition's guard.

        Args:
            t: The guarded transition.

        Returns:
            A callback for the underlying state-machine engine.
        """
        expr = t.guard or ""  # only called for guarded transitions

        def guard(data: dict[str, Any] | None = None) -> bool:
            outcome = evaluate(expr, data or {})
            self.coverage.guards.add(guard_key(self._before, t, outcome))
            return outcome

        return guard

    def _recorder(self, t: Transition) -> Callable[[dict[str, Any] | None], None]:
        """Create a callback that records a fired transition and its coverage.

        Args:
            t: The transition to record.

        Returns:
            A callback for the underlying state-machine engine.
        """

        def record(data: dict[str, Any] | None = None) -> None:
            self._fired = t
            self.coverage.transitions.add(transition_key(self._before, t))

        return record

    @property
    def state(self) -> str:
        """Return the machine's current state name.

        Returns:
            The current state.
        """
        return self._model.state

    def set_state(self, name: str) -> None:
        """Set the current state without firing a transition.

        Args:
            name: The state to enter.
        """
        self._machine.set_state(name, model=self._model)
        self.coverage.states.add(name)

    def send(self, event: str, data: dict[str, Any] | None = None) -> Step:
        """Process one event and report whether a transition fired.

        Args:
            event: The event to send.
            data: Values available to transition guards.

        Returns:
            The event's source state, outcome, and resulting state.

        Raises:
            CommandError: If the event is unknown or a guard can't be evaluated.
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
