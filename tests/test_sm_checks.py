"""Test structural analysis independently of plugin loading and transports."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rigbox.plugins.statemachine.check import RULES, GraphIndex, check
from rigbox.plugins.statemachine.schema import MachineSpec

EXAMPLES = Path(__file__).parent.parent / "examples"


def machine(**overrides: object) -> MachineSpec:
    data: dict[str, object] = {
        "name": "M",
        "initial": "a",
        "states": [{"name": "a"}, {"name": "b"}],
        "events": [{"name": "go"}, {"name": "back"}],
        "transitions": [
            {"from": "a", "event": "go", "to": "b"},
            {"from": "b", "event": "back", "to": "a"},
        ],
    }
    data.update(overrides)
    return MachineSpec.model_validate(data)


def test_example_checks_clean() -> None:
    assert check(MachineSpec.load_from_file(EXAMPLES / "power_mode.yaml")) == []


def test_index_keeps_parallel_edges_order_and_manifest_unchanged() -> None:
    spec = machine(
        transitions=[
            {"from": "a", "event": "go", "to": "b", "guard": "x > 1"},
            {"from": "a", "event": "go", "to": "b"},
        ],
    )
    before = spec.model_dump()
    index = GraphIndex.build(spec)
    assert index.graph.number_of_edges("a", "b") == 2
    assert index.by_source_event[("a", "go")] == spec.transitions
    for position, transition in enumerate(spec.transitions):
        assert index.graph.edges["a", "b", position]["transition"] is transition
    check(spec)
    assert spec.model_dump() == before


def test_index_expands_wildcards_and_multiple_sources_in_declaration_order() -> None:
    spec = machine(
        states=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
        transitions=[
            {"from": "*", "event": "go", "to": "b", "guard": "x > 1"},
            {"from": ["a", "c"], "event": "go", "to": "a"},
        ],
    )
    index = GraphIndex.build(spec)
    assert index.graph.number_of_edges() == 5
    assert index.by_source_event[("a", "go")] == spec.transitions
    assert index.by_source_event[("c", "go")] == spec.transitions
    assert index.by_source_event[("b", "go")] == spec.transitions[:1]


def test_index_uses_declared_initial_state_and_reverse_reachability() -> None:
    spec = machine(
        initial="b",
        states=[{"name": "a"}, {"name": "b"}, {"name": "safe", "safe": True}, {"name": "isolated", "safe": True}],
        transitions=[{"from": "a", "event": "go", "to": "b"}, {"from": "b", "event": "back", "to": "safe"}],
    )
    index = GraphIndex.build(spec)
    assert index.reachable == {"b", "safe"}
    assert index.can_reach_safe == {"a", "b", "safe", "isolated"}


def test_existing_findings_keep_identifiers_severity_and_output_fields() -> None:
    spec = machine(
        states=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
        transitions=[{"from": "a", "event": "go", "to": "b"}],
    )
    issues = check(spec)
    assert [(issue.rule, issue.subject, issue.level) for issue in issues] == [
        ("unreachable-state", "c", "warning"),
        ("dead-end", "b", "warning"),
        ("unused-event", "back", "warning"),
    ]
    assert all(set(issue.model_dump()) == {"level", "rule", "subject", "message"} for issue in issues)


def test_safe_dead_end_is_allowed() -> None:
    spec = machine(
        states=[{"name": "a"}, {"name": "b", "safe": True}],
        transitions=[{"from": "a", "event": "go", "to": "b"}],
    )
    assert not any(issue.rule in {"dead-end", "no-safe-path"} for issue in check(spec))


@pytest.mark.parametrize("guarded_first", [False, True])
def test_shadowing_respects_guard_and_declaration_order(guarded_first: bool) -> None:
    transitions: list[dict[str, object]] = [
        {"from": "*", "event": "go", "to": "b"},
        {"from": ["a", "b"], "event": "go", "to": "a"},
    ]
    if guarded_first:
        transitions[0]["guard"] = "x > 1"
    issues = [issue for issue in check(machine(transitions=transitions)) if issue.rule == "shadowed-transition"]
    assert [issue.subject for issue in issues] == ([] if guarded_first else ["a --go-->", "b --go-->"])
    assert all(issue.level == "error" for issue in issues)


def test_safe_paths_include_guarded_edges_but_not_unreachable_states() -> None:
    spec = machine(
        states=[{"name": "a"}, {"name": "b", "safe": True}, {"name": "unreachable"}],
        transitions=[{"from": "a", "event": "go", "to": "b", "guard": "False"}],
    )
    assert not any(issue.rule == "no-safe-path" for issue in check(spec))


def test_closed_component_reports_once_with_sorted_members() -> None:
    spec = machine(
        states=[{"name": "a", "safe": True}, {"name": "c"}, {"name": "b"}],
        transitions=[
            {"from": "a", "event": "go", "to": "c"},
            {"from": "c", "event": "go", "to": "b"},
            {"from": "b", "event": "back", "to": "c"},
        ],
    )
    issues = check(spec)
    traps = [issue for issue in issues if issue.rule == "trapping-component"]
    assert [(issue.subject, issue.level) for issue in traps] == [("b, c", "warning")]
    assert {issue.subject for issue in issues if issue.rule == "no-safe-path"} == {"b", "c"}
    assert issues == check(spec)


@pytest.mark.parametrize("safe", [False, True])
def test_entered_self_loop_is_a_trap_unless_safe(safe: bool) -> None:
    spec = machine(
        states=[{"name": "a"}, {"name": "b", "safe": safe}],
        transitions=[{"from": "a", "event": "go", "to": "b"}, {"from": "b", "event": "back", "to": "b"}],
    )
    traps = [issue for issue in check(spec) if issue.rule == "trapping-component"]
    assert [issue.subject for issue in traps] == ([] if safe else ["b"])


@pytest.mark.parametrize("guard", [None, "False"])
def test_structural_exit_prevents_trap_even_when_guarded(guard: str | None) -> None:
    spec = machine(
        states=[{"name": "a"}, {"name": "b"}, {"name": "safe", "safe": True}],
        transitions=[
            {"from": "a", "event": "go", "to": "b"},
            {"from": "b", "event": "go", "to": "b"},
            {"from": "b", "event": "back", "to": "safe", "guard": guard},
        ],
    )
    assert not any(issue.rule == "trapping-component" for issue in check(spec))


def test_normal_operating_cycle_and_unreachable_cycles_are_not_traps() -> None:
    spec = machine(
        states=[{"name": "a"}, {"name": "b"}, {"name": "unreachable"}],
        transitions=[
            {"from": "a", "event": "go", "to": "b"},
            {"from": "b", "event": "back", "to": "a"},
            {"from": "unreachable", "event": "go", "to": "unreachable"},
        ],
    )
    assert not any(issue.rule == "trapping-component" for issue in check(spec))


def test_required_event_missing_is_an_error_but_other_events_are_optional() -> None:
    spec = machine(states=[{"name": "a", "required_events": ["go", "back"]}, {"name": "b"}])
    issues = [issue for issue in check(spec) if issue.rule == "missing-required-event"]
    assert [(issue.subject, issue.level) for issue in issues] == [("a --back-->", "error")]


@pytest.mark.parametrize("source", ["*", ["a", "b"]])
def test_required_events_accept_wildcard_list_sources_and_guarded_transitions(source: str | list[str]) -> None:
    spec = machine(
        states=[{"name": "a", "required_events": ["go"]}, {"name": "b", "required_events": ["go"]}],
        transitions=[{"from": source, "event": "go", "to": "a", "guard": "False"}],
    )
    assert not any(issue.rule == "missing-required-event" for issue in check(spec))


@pytest.mark.parametrize("required_events", [["missing"], ["go", "go"]])
def test_required_events_reject_unknown_and_duplicate_names(required_events: list[str]) -> None:
    with pytest.raises(ValidationError, match="unknown event|duplicate required events"):
        machine(states=[{"name": "a", "required_events": required_events}, {"name": "b"}])


def test_rules_have_unique_explicit_identifiers_and_can_run_independently() -> None:
    assert len({rule.name for rule in RULES}) == len(RULES)
    spec = machine(states=[{"name": "a"}, {"name": "b"}], transitions=[])
    index = GraphIndex.build(spec)
    findings = next(rule for rule in RULES if rule.name == "unreachable-state").evaluate(index)
    assert [finding.subject for finding in findings] == ["b"]
