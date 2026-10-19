import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from rigbox.constants import RenderFormat, RenderLevel
from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.render import RigboxCmdResult
from rigbox.plugins.statemachine.check import check
from rigbox.plugins.statemachine.engine import Simulator
from rigbox.plugins.statemachine.schema import MachineSpec

EXAMPLES = Path(__file__).parent.parent / "examples"
POWER_MODE = str(EXAMPLES / "power_mode.yaml")
POWER_REPORT = str(EXAMPLES / "power_report.yaml")


class _TerminalStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def spec(**overrides: object) -> MachineSpec:
    data: dict[str, Any] = {
        "name": "M",
        "version": "1.0",
        "initial": "a",
        "states": [{"name": "a"}, {"name": "b"}],
        "events": [{"name": "go"}, {"name": "back"}],
        "transitions": [{"from": "a", "event": "go", "to": "b"}, {"from": "b", "event": "back", "to": "a"}],
    }
    data.update(overrides)
    return MachineSpec.model_validate(data)


def _data(result: RigboxCmdResult) -> dict[str, Any]:
    assert isinstance(result.structured_data, dict)
    return result.structured_data


def _check_formats(result: RigboxCmdResult) -> None:
    assert result.render(RenderFormat.PLAINTEXT, io.StringIO())
    assert result.render(RenderFormat.RICH, io.StringIO())
    assert json.loads(result.render(RenderFormat.JSON)) == [_data(result)]
    assert json.loads(result.render(RenderFormat.NDJSON)) == _data(result)


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
    assert check(MachineSpec.load_from_file(Path(POWER_MODE))) == []


# --- engine -------------------------------------------------------------------------------------------------


def test_simulator_fires_guards_and_reports_actions() -> None:
    sim = Simulator(MachineSpec.load_from_file(Path(POWER_MODE)))
    assert sim.state == "idle"
    step = sim.send("start")
    assert (step.fired, step.to_state, step.actions) == (True, "running", ["enable_output"])
    blocked = sim.send("overheat", {"temperature": 85})
    assert (blocked.fired, blocked.reason, sim.state) == (False, "guard failed", "running")
    assert sim.send("overheat", {"temperature": 95}).to_state == "fault"
    nope = sim.send("start")
    assert (nope.fired, nope.reason) == (False, "no transition from fault")


def test_simulator_errors() -> None:
    sim = Simulator(MachineSpec.load_from_file(Path(POWER_MODE)))
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
    text = MachineSpec.load_from_file(Path(POWER_MODE)).to_mermaid()
    assert text.splitlines()[0] == "stateDiagram-v2"
    assert text.splitlines()[1].strip() == "[*] --> idle"
    assert "running --> fault : overheat [temperature #gt; 90] / disable_output" in text
    assert "class idle safe" in text and "<" not in text and ">" not in text.replace("-->", "")


def test_render_command_writes_file(ctx: Context, tmp_path: Path) -> None:
    out = tmp_path / "m.mmd"
    result = ctx.run("sm.render", {"machine": POWER_MODE, "out": str(out)})
    assert out.read_text(encoding="utf-8").startswith("stateDiagram-v2")
    assert _data(result) == {"written": str(out)}
    _check_formats(result)


def test_render_reports_write_failure(ctx: Context, tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="cannot write Mermaid"):
        ctx.run("sm.render", {"machine": POWER_MODE, "out": str(tmp_path)})


def test_mermaid_result_contains_diagram_in_all_formats(ctx: Context) -> None:
    result = ctx.run("sm.render", {"machine": POWER_MODE})
    assert result.text == _data(result)["mermaid"]
    assert result.render(RenderFormat.PLAINTEXT).startswith("stateDiagram-v2")
    _check_formats(result)


# --- commands -----------------------------------------------------------------------------------------------


def test_simulate_command_parses_event_data(ctx: Context) -> None:
    result = ctx.run("sm.simulate", {"machine": POWER_MODE, "steps": "[start, {overheat: {temperature: 95}}]"})
    rows = _data(result)["steps"]
    assert [(r["step"], r["event"], r["to_state"]) for r in rows] == [(1, "start", "running"), (2, "overheat", "fault")]
    assert result.text and "enable_output" in result.text
    _check_formats(result)
    with pytest.raises(CommandError, match="steps must be a list"):
        ctx.run("sm.simulate", {"machine": POWER_MODE, "steps": "start"})
    with pytest.raises(CommandError, match="not found"):
        ctx.run("sm.show", {"machine": "nope"})


def test_check_command_reports_success_when_no_findings(ctx: Context) -> None:
    result = ctx.run("sm.check", {"machine": POWER_MODE})
    assert _data(result) == {"findings": []}
    assert result.level == RenderLevel.SUCCESS
    assert result.text == "No structural issues found"
    _check_formats(result)


def test_check_command_renders_green_success(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _TerminalStream()
    result = ctx.run("sm.check", {"machine": POWER_MODE})
    text = result.render(RenderFormat.RICH, stream)
    assert "No structural issues found" in text
    assert "\x1b[32m" in text


@pytest.mark.parametrize("has_error", [False, True])
def test_check_command_serializes_findings_and_preserves_severity(ctx: Context, tmp_path: Path, has_error: bool) -> None:
    machine = spec(transitions=[])
    if has_error:
        machine.states[0].required_events = ["go"]
    path = tmp_path / "findings.yaml"
    path.write_text(yaml.safe_dump(machine.model_dump(mode="json", by_alias=True)), encoding="utf-8")
    result = ctx.run("sm.check", {"machine": str(path)})
    assert result.level == (RenderLevel.ERROR if has_error else RenderLevel.WARNING)
    assert _data(result)["findings"]
    assert result.text and "unused-event" in result.text
    _check_formats(result)


def test_show_command(ctx: Context) -> None:
    result = ctx.run("sm.show", {"machine": POWER_MODE})
    rows = _data(result)["transitions"]
    assert rows[2] == {"from": "running", "event": "overheat", "guard": "temperature > 90", "to": "fault", "actions": "disable_output"}
    assert "temperature > 90" in result.render(RenderFormat.RICH, io.StringIO())
    _check_formats(result)


def test_empty_simulation_and_replay_results(ctx: Context, tmp_path: Path) -> None:
    simulation = ctx.run("sm.simulate", {"machine": POWER_MODE, "steps": "[]"})
    assert _data(simulation) == {"steps": []}
    _check_formats(simulation)
    trace = tmp_path / "empty.ndjson"
    trace.write_text("", encoding="utf-8")
    replay = ctx.run("sm.replay", {"machine": POWER_MODE, "trace": str(trace), "only_problems": True})
    assert _data(replay)["rows"] == []
    assert replay.level == RenderLevel.SUCCESS
    _check_formats(replay)


def test_show_machine_without_transitions(ctx: Context, tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text(yaml.safe_dump(spec(transitions=[]).model_dump(mode="json", by_alias=True)), encoding="utf-8")
    result = ctx.run("sm.show", {"machine": str(path)})
    assert _data(result) == {"transitions": []}
    assert result.text == "M: no transitions defined"
    _check_formats(result)


def test_replay_guard_error_is_a_problem(ctx: Context, tmp_path: Path) -> None:
    machine = spec(
        events=[{"name": "go", "match": "True"}],
        transitions=[{"from": "a", "event": "go", "to": "b", "guard": "missing > 0"}],
    )
    path = tmp_path / "guard.yaml"
    path.write_text(yaml.safe_dump(machine.model_dump(mode="json", by_alias=True)), encoding="utf-8")
    trace = tmp_path / "guard.ndjson"
    trace.write_text(json.dumps({"decoded": {}}) + "\n", encoding="utf-8")
    result = ctx.run("sm.replay", {"machine": str(path), "trace": str(trace), "only_problems": True})
    assert result.level == RenderLevel.ERROR
    assert _data(result)["errors"] == 1
    assert len(_data(result)["rows"]) == 1
    assert _data(result)["rows"][0]["reason"].startswith("error:")
    _check_formats(result)


def test_coverage_and_successful_fuzz_results(ctx: Context, tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.json"
    ctx.run("sm.simulate", {"machine": POWER_MODE, "steps": "[start]", "coverage_file": str(coverage_file)})
    coverage = ctx.run("sm.coverage", {"machine": POWER_MODE, "file": str(coverage_file)})
    states = next(row for row in _data(coverage)["coverage"] if row["kind"] == "states")
    assert states["covered"] == 2
    assert coverage.text and "states: 2/3" in coverage.text
    _check_formats(coverage)
    fuzz_result = ctx.run("sm.fuzz", {"machine": POWER_MODE, "seed": 1, "runs": 1, "length": 1})
    assert fuzz_result.text and "seed 1" in fuzz_result.text
    assert len(_data(fuzz_result)["coverage"]) == 3
    _check_formats(fuzz_result)


# --- trace replay, end to end: device.send -> device.capture -> sm.replay -------------------------------------

PACKETS = [
    ("idle", 25.0),  # 1: cooled matches, no transition from idle; device agrees
    ("running", 25.0),  # 2: device says running but model is idle -> diverged, model adopts 'running'
    ("running", 85.0),  # 3: overheat matches, guard (>90) fails
    ("fault", 95.0),  # 4: overheat fires -> fault; device agrees
    ("fault", 40.0),  # 5: cooled fires -> idle, but device still says fault -> diverged
    ("bogus", 40.0),  # 6: cooled; device reports a state that doesn't exist -> diverged
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
    result = ctx.run("sm.replay", {"machine": POWER_MODE, "trace": str(trace)})
    rows = _data(result)["rows"]
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
    assert result.level == RenderLevel.WARNING
    assert result.text and "3 diverged, 1 skipped" in result.text
    _check_formats(result)


def test_replay_only_problems_filter(ctx: Context, trace: Path) -> None:
    result = ctx.run("sm.replay", {"machine": POWER_MODE, "trace": str(trace), "only_problems": True})
    rows = _data(result)["rows"]
    assert [r["n"] for r in rows] == [2, 5, 6, 7]
    assert _data(result)["records"] == 7
