import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.plugins.statemachine.check import check
from rigbox.plugins.statemachine.engine import Simulator
from rigbox.plugins.statemachine.render import to_mermaid
from rigbox.plugins.statemachine.schema import MachineSpec, load_machine

EXAMPLES = Path(__file__).parent.parent / "examples"
POWER_MODE = str(EXAMPLES / "power_mode.yaml")
POWER_REPORT = str(EXAMPLES / "power_report.yaml")


def spec(**overrides: object) -> MachineSpec:
    data: dict[str, Any] = {
        "name": "M",
        "initial": "a",
        "states": [{"name": "a"}, {"name": "b"}],
        "events": [{"name": "go"}, {"name": "back"}],
        "transitions": [{"from": "a", "event": "go", "to": "b"}, {"from": "b", "event": "back", "to": "a"}],
    }
    data.update(overrides)
    return MachineSpec.model_validate(data)


@pytest.fixture
def ctx() -> Iterator[Context]:
    reg = Registry()
    load_all(reg)
    c = Context(registry=reg)
    yield c
    c.close()


# --- schema -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides",
    [
        {"initial": "zzz"},
        {"states": [{"name": "a"}, {"name": "a"}]},
        {"events": [{"name": "go"}, {"name": "go"}]},
        {"states": [{"name": "1bad"}]},
        {"transitions": [{"from": "a", "event": "go", "to": "nope"}]},
        {"transitions": [{"from": "nope", "event": "go", "to": "a"}]},
        {"transitions": [{"from": "a", "event": "nope", "to": "b"}]},
        {"transitions": [{"from": "a", "event": "go", "to": "b", "guard": "temperature >"}]},
        {"events": [{"name": "go", "match": "x ==="}]},
        {"states": []},
    ],
)
def test_schema_rejects_bad_machines(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        spec(**overrides)


def test_wildcard_and_list_sources() -> None:
    m = spec(transitions=[{"from": "*", "event": "go", "to": "b"}, {"from": ["a", "b"], "event": "back", "to": "a"}])
    assert [m.sources(t) for t in m.transitions] == [["a", "b"], ["a", "b"]]


def test_example_machine_loads_and_checks_clean() -> None:
    assert check(load_machine(Path(POWER_MODE))) == []


# --- engine -------------------------------------------------------------------------------------------------

def test_simulator_fires_guards_and_reports_actions() -> None:
    sim = Simulator(load_machine(Path(POWER_MODE)))
    assert sim.state == "idle"
    step = sim.send("start")
    assert (step.fired, step.to_state, step.actions) == (True, "running", ["enable_output"])
    blocked = sim.send("overheat", {"temperature": 85})
    assert (blocked.fired, blocked.reason, sim.state) == (False, "guard failed", "running")
    assert sim.send("overheat", {"temperature": 95}).to_state == "fault"
    nope = sim.send("start")
    assert (nope.fired, nope.reason) == (False, "no transition from fault")


def test_simulator_errors() -> None:
    sim = Simulator(load_machine(Path(POWER_MODE)))
    with pytest.raises(CommandError, match="unknown event"):
        sim.send("explode")
    sim.send("start")
    with pytest.raises(CommandError, match="cannot evaluate"):
        sim.send("overheat", {})  # guard needs `temperature`


def test_guard_expressions_cannot_run_arbitrary_code() -> None:
    m = spec(transitions=[{"from": "a", "event": "go", "to": "b", "guard": "__import__('os').getcwd()"}])
    with pytest.raises(CommandError, match="cannot evaluate"):
        Simulator(m).send("go")


def test_wildcard_transition_fires_from_every_state() -> None:
    sim = Simulator(spec(transitions=[{"from": "*", "event": "go", "to": "b"}]))
    assert sim.send("go").fired and sim.send("go").fired  # b -> b


# --- property checks ----------------------------------------------------------------------------------------

def rules(m: MachineSpec) -> set[tuple[str, str]]:
    return {(i.rule, i.subject) for i in check(m)}


def test_check_unreachable_dead_end_and_unused_event() -> None:
    m = spec(
        states=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
        transitions=[{"from": "a", "event": "go", "to": "b"}],
    )
    assert rules(m) == {("unreachable-state", "c"), ("dead-end", "b"), ("unused-event", "back")}


def test_check_dead_end_allowed_when_safe() -> None:
    m = spec(states=[{"name": "a"}, {"name": "b", "safe": True}], transitions=[{"from": "a", "event": "go", "to": "b"}])
    assert ("dead-end", "b") not in rules(m)


def test_check_shadowed_transition() -> None:
    m = spec(
        transitions=[
            {"from": "a", "event": "go", "to": "b"},
            {"from": "a", "event": "go", "to": "a", "guard": "x > 1"},
        ]
    )
    assert ("shadowed-transition", "a --go-->") in rules(m)
    guarded_first = spec(
        transitions=[
            {"from": "a", "event": "go", "to": "a", "guard": "x > 1"},
            {"from": "a", "event": "go", "to": "b"},
        ]
    )
    assert not any(r == "shadowed-transition" for r, _ in rules(guarded_first))


def test_check_no_safe_path() -> None:
    m = spec(
        states=[{"name": "a", "safe": True}, {"name": "b"}, {"name": "trap"}],
        transitions=[
            {"from": "a", "event": "go", "to": "b"},
            {"from": "b", "event": "go", "to": "trap"},
            {"from": "trap", "event": "back", "to": "trap"},
        ],
    )
    found = rules(m)
    assert ("no-safe-path", "trap") in found and ("no-safe-path", "b") in found and ("no-safe-path", "a") not in found


# --- rendering ----------------------------------------------------------------------------------------------

def test_mermaid_output() -> None:
    text = to_mermaid(load_machine(Path(POWER_MODE)))
    assert text.startswith("stateDiagram-v2\n    [*] --> idle\n")
    assert "running --> fault : overheat [temperature #gt; 90] / disable_output" in text
    assert "class idle safe" in text and "<" not in text and ">" not in text.replace("-->", "")


def test_render_command_writes_file(ctx: Context, tmp_path: Path) -> None:
    out = tmp_path / "m.mmd"
    ctx.run("sm.render", {"machine": POWER_MODE, "out": str(out)})
    assert out.read_text(encoding="utf-8").startswith("stateDiagram-v2")


# --- commands -----------------------------------------------------------------------------------------------

def test_simulate_command_parses_event_data(ctx: Context) -> None:
    rows = ctx.run("sm.simulate", {"machine": POWER_MODE, "steps": "[start, {overheat: {temperature: 95}}]"})
    assert [(r["step"], r["event"], r["to_state"]) for r in rows] == [(1, "start", "running"), (2, "overheat", "fault")]
    with pytest.raises(CommandError, match="steps must be a list"):
        ctx.run("sm.simulate", {"machine": POWER_MODE, "steps": "start"})
    with pytest.raises(CommandError, match="not found"):
        ctx.run("sm.show", {"machine": "nope"})


def test_show_command(ctx: Context) -> None:
    rows = ctx.run("sm.show", {"machine": POWER_MODE})
    assert rows[2] == {"from": "running", "event": "overheat", "guard": "temperature > 90", "to": "fault",
                       "actions": "disable_output"}


# --- trace replay, end to end: device.send -> device.capture -> sm.replay -------------------------------------

PACKETS = [
    ("idle", 25.0),      # 1: cooled matches, no transition from idle; device agrees
    ("running", 25.0),   # 2: device says running but model is idle -> diverged, model adopts 'running'
    ("running", 85.0),   # 3: overheat matches, guard (>90) fails
    ("fault", 95.0),     # 4: overheat fires -> fault; device agrees
    ("fault", 40.0),     # 5: cooled fires -> idle, but device still says fault -> diverged
    ("bogus", 40.0),     # 6: cooled; device reports a state that doesn't exist -> diverged
]


@pytest.fixture
def trace(ctx: Context, tmp_path: Path) -> Path:
    ctx.run("device.connect", {"name": "d", "url": "loop://", "timeout": 0.1})
    for mode, temp in PACKETS:
        values = f"{{mode: {mode}, temperature: {temp}}}"
        ctx.run("device.send", {"name": "d", "manifest": POWER_REPORT, "values": values})
    path = tmp_path / "trace.ndjson"
    capture_args = {"name": "d", "manifest": POWER_REPORT, "count": len(PACKETS), "seconds": 5, "out": str(path)}
    ctx.run("device.capture", capture_args)
    with path.open("a", encoding="utf-8") as f:  # a record the capture could not decode
        f.write(json.dumps({"ts": "t", "dir": "rx", "hex": "00", "error": "partial packet (1 of 12 bytes)"}) + "\n")
    return path


def test_replay_flags_divergence_and_resyncs(ctx: Context, trace: Path) -> None:
    rows = ctx.run("sm.replay", {"machine": POWER_MODE, "trace": str(trace)})
    assert len(rows) == 7
    by_n = {r["n"]: r for r in rows}
    assert [by_n[n]["diverged"] for n in range(1, 8)] == [False, True, False, False, True, True, False]
    assert by_n[1]["reason"] == "no transition from idle"
    assert (by_n[2]["event"], by_n[2]["to_state"], by_n[2]["device_state"]) == ("cooled", "idle", "running")
    assert by_n[3]["reason"] == "guard failed" and not by_n[3]["fired"]  # model was resynced to 'running'
    assert by_n[4]["fired"] and by_n[4]["to_state"] == "fault"
    assert by_n[5]["fired"] and by_n[5]["to_state"] == "idle" and by_n[5]["device_state"] == "fault"
    assert "unknown device state" in by_n[6]["reason"]
    assert by_n[7]["reason"].startswith("skipped: partial packet")


def test_replay_only_problems_filter(ctx: Context, trace: Path) -> None:
    rows = ctx.run("sm.replay", {"machine": POWER_MODE, "trace": str(trace), "only_problems": True})
    assert [r["n"] for r in rows] == [2, 5, 6, 7]
