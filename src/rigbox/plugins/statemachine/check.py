"""Property checks on a machine's structure (guards are ignored: every transition counts as possible)."""

from __future__ import annotations

from collections import defaultdict, deque

from pydantic import BaseModel

from rigbox.plugins.statemachine.schema import MachineSpec, Transition


class Issue(BaseModel):
    """One finding from `check`."""

    level: str  # "error" | "warning"
    rule: str
    subject: str
    message: str


def _reachable(start: list[str], edges: dict[str, set[str]]) -> set[str]:
    seen, todo = set(start), deque(start)
    while todo:
        for nxt in edges[todo.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def check(spec: MachineSpec) -> list[Issue]:
    """Return the structural problems found in `spec`."""
    issues: list[Issue] = []
    forward: dict[str, set[str]] = defaultdict(set)
    backward: dict[str, set[str]] = defaultdict(set)
    by_source_event: dict[tuple[str, str], list[Transition]] = defaultdict(list)
    for t in spec.transitions:
        for src in spec.sources(t):
            forward[src].add(t.target)
            backward[t.target].add(src)
            by_source_event[(src, t.event)].append(t)

    reachable = _reachable([spec.initial], forward)
    safe = [s.name for s in spec.states if s.safe]

    for name in spec.state_names:
        if name not in reachable:
            issues.append(Issue(level="warning", rule="unreachable-state", subject=name,
                                message=f"'{name}' cannot be reached from '{spec.initial}'"))
        elif not forward[name] and name not in safe:
            issues.append(Issue(level="warning", rule="dead-end", subject=name,
                                message=f"'{name}' has no outgoing transitions and is not marked safe"))

    for (src, event), ts in by_source_event.items():
        for i, t in enumerate(ts[:-1]):
            if t.guard is None:  # an unguarded transition always fires, so later ones can never run
                later = len(ts) - i - 1
                message = f"unguarded transition to '{t.target}' means {later} later one(s) never fire"
                issues.append(Issue(level="error", rule="shadowed-transition", subject=f"{src} --{event}-->",
                                    message=message))
                break

    used = {t.event for t in spec.transitions}
    for e in spec.events:
        if e.name not in used:
            issues.append(Issue(level="warning", rule="unused-event", subject=e.name,
                                message=f"event '{e.name}' is not used by any transition"))

    if safe:
        can_reach_safe = _reachable(safe, backward)
        for name in spec.state_names:
            if name in reachable and name not in can_reach_safe:
                issues.append(Issue(level="error", rule="no-safe-path", subject=name,
                                    message=f"no path from '{name}' to any safe state"))
    return issues
