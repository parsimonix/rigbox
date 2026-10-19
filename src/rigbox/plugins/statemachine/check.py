"""Check state-machine structure and report findings for specific properties.

The module builds a shared graph index, then evaluates independent rules against it. These checks ignore guards,
so they describe structural possibilities rather than runtime behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import networkx as nx
from pydantic import BaseModel

from rigbox.plugins.statemachine.schema import MachineSpec, Transition


class Issue(BaseModel):
    """Represent one finding from a structural check."""

    level: Literal["error", "warning"]
    rule: str
    subject: str
    message: str


@dataclass(frozen=True)
class GraphIndex:
    """Index a machine's possible transitions without evaluating guards."""

    spec: MachineSpec
    graph: nx.MultiDiGraph
    by_source_event: dict[tuple[str, str], list[Transition]]
    reachable: frozenset[str]
    safe: frozenset[str]
    can_reach_safe: frozenset[str]

    @classmethod
    def build(cls, spec: MachineSpec) -> GraphIndex:
        """Build reusable indexes for structural checks while preserving transition order and parallel edges.

        Args:
            spec: The validated state-machine specification.

        Returns:
            The graph and lookup indexes used by the checks.
        """
        graph = nx.MultiDiGraph()
        graph.add_nodes_from(spec.state_names)
        by_source_event: dict[tuple[str, str], list[Transition]] = {}
        for index, transition in enumerate(spec.transitions):
            for source in spec.sources(transition):
                graph.add_edge(source, transition.target, key=index, transition=transition)
                by_source_event.setdefault((source, transition.event), []).append(transition)
        reachable = frozenset({spec.initial} | nx.descendants(graph, spec.initial))
        safe = frozenset(state.name for state in spec.states if state.safe)
        can_reach_safe = frozenset(name for layer in nx.bfs_layers(graph.reverse(copy=False), sorted(safe)) for name in layer)
        return cls(spec, graph, by_source_event, reachable, safe, can_reach_safe)


@dataclass(frozen=True)
class Finding:
    """Describe a detected condition independently of its rule's severity."""

    subject: str
    message: str


@dataclass(frozen=True)
class Rule:
    """Describe one independent check with a stable identifier and default severity."""

    name: str
    description: str
    default_level: Literal["error", "warning"]
    evaluate: Callable[[GraphIndex], list[Finding]]


def _check_unreachable(index: GraphIndex) -> list[Finding]:
    return [Finding(name, f"'{name}' cannot be reached from '{index.spec.initial}'") for name in index.spec.state_names if name not in index.reachable]


def _check_dead_ends(index: GraphIndex) -> list[Finding]:
    return [
        Finding(name, f"'{name}' has no outgoing transitions and is not marked safe")
        for name in index.spec.state_names
        if name in index.reachable and not index.graph.adj[name] and name not in index.safe
    ]


def _check_shadowed(index: GraphIndex) -> list[Finding]:
    findings: list[Finding] = []
    for (source, event), transitions in index.by_source_event.items():
        for position, transition in enumerate(transitions[:-1]):
            if transition.guard is None:
                later = len(transitions) - position - 1
                message = f"unguarded transition to '{transition.target}' means {later} later one(s) never fire"
                findings.append(Finding(f"{source} --{event}-->", message))
                break
    return findings


def _check_unused_events(index: GraphIndex) -> list[Finding]:
    used = {transition.event for transition in index.spec.transitions}
    return [Finding(event.name, f"event '{event.name}' is not used by any transition") for event in index.spec.events if event.name not in used]


def _check_safe_paths(index: GraphIndex) -> list[Finding]:
    if not index.safe:
        return []
    return [
        Finding(name, f"no path from '{name}' to any safe state")
        for name in index.spec.state_names
        if name in index.reachable and name not in index.can_reach_safe
    ]


def _check_trapping_components(index: GraphIndex) -> list[Finding]:
    findings: list[Finding] = []
    components = sorted(sorted(component) for component in nx.strongly_connected_components(index.graph))
    for members in components:
        component = set(members)
        if index.spec.initial in component or not component.intersection(index.reachable):
            continue
        if component.intersection(index.safe):
            continue
        if len(members) == 1 and not index.graph.has_edge(members[0], members[0]):
            continue
        if any(target not in component for source in members for target in index.graph.successors(source)):
            continue
        subject = ", ".join(members)
        findings.append(Finding(subject, f"closed cycle among {subject} has no structural exit or safe state (guards ignored)"))
    return findings


def _check_required_events(index: GraphIndex) -> list[Finding]:
    return [
        Finding(f"{state.name} --{event}-->", f"state '{state.name}' requires an outgoing transition for event '{event}'")
        for state in index.spec.states
        for event in state.required_events
        if (state.name, event) not in index.by_source_event
    ]


RULES: tuple[Rule, ...] = (
    Rule("unreachable-state", "Find states unreachable from the initial state.", "warning", _check_unreachable),
    Rule("dead-end", "Find reachable non-safe states with no outgoing transitions.", "warning", _check_dead_ends),
    Rule("shadowed-transition", "Find transitions hidden by an earlier unguarded transition.", "error", _check_shadowed),
    Rule("unused-event", "Find events not used by any transition.", "warning", _check_unused_events),
    Rule("no-safe-path", "Find reachable states without a structural path to safety.", "error", _check_safe_paths),
    Rule("trapping-component", "Find entered closed cycles without a safe state.", "warning", _check_trapping_components),
    Rule("missing-required-event", "Find required events without a declared outgoing transition.", "error", _check_required_events),
)


def check(spec: MachineSpec) -> list[Issue]:
    """Evaluate each structural rule and return its findings.

    Args:
        spec: The validated state-machine specification.

    Returns:
        Findings produced by the structural checks.
    """
    index = GraphIndex.build(spec)
    return [
        Issue(level=rule.default_level, rule=rule.name, subject=finding.subject, message=finding.message) for rule in RULES for finding in rule.evaluate(index)
    ]
