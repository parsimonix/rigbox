import json
import re
from pathlib import Path
from typing import Any

import pytest

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.plugins.manifests.schema import StructManifest
from rigbox.plugins.statemachine.coverage import Coverage, summary
from rigbox.plugins.statemachine.engine import Simulator
from rigbox.plugins.statemachine.fuzz import format_steps, value_pools
from rigbox.plugins.statemachine.schema import MachineSpec

POWER_MODE = Path(__file__).parent.parent / "examples" / "power_mode.yaml"


def spec(**overrides: object) -> MachineSpec:
    data: dict[str, Any] = {
        "name": "M",
        "initial": "a",
        "states": [{"name": "a"}, {"name": "b"}],
        "events": [{"name": "go"}, {"name": "back"}],
        "transitions": [
            {"from": "a", "event": "go", "to": "b", "guard": "level > 10"},
            {"from": "b", "event": "back", "to": "a"},
        ],
    }
    data.update(overrides)
    return MachineSpec.model_validate(data)


@pytest.fixture
def ctx() -> Context:
    reg = Registry()
    load_all(reg)
    return Context(registry=reg)


# --- coverage -----------------------------------------------------------------------------------------------


def test_simulator_tracks_states_transitions_and_guard_outcomes() -> None:
    sim = Simulator(spec())
    assert sim.coverage.states == {"a"}
    sim.send("go", {"level": 5})  # guard false
    assert sim.coverage.guards == {"a --go[level > 10]--> b guard=false"}
    assert sim.coverage.transitions == set()
    sim.send("go", {"level": 50})  # guard true, fires
    sim.send("back")
    cov = sim.coverage
    assert cov.states == {"a", "b"}
    assert cov.transitions == {"a --go[level > 10]--> b", "b --back--> a"}
    assert cov.guards == {"a --go[level > 10]--> b guard=false", "a --go[level > 10]--> b guard=true"}


def test_summary_lists_uncovered_and_ignores_stale_items() -> None:
    m = spec()
    cov = Coverage(machine="M", states={"a", "gone"}, transitions={"stale --x--> y"})
    rows = {r["kind"]: r for r in summary(m, cov)}
    assert (rows["states"]["covered"], rows["states"]["total"], rows["states"]["uncovered"]) == (1, 2, ["b"])
    assert rows["transitions"]["covered"] == 0 and rows["transitions"]["percent"] == 0.0
    assert len(rows["guards"]["uncovered"]) == 2


def test_coverage_merge_and_file_roundtrip(tmp_path: Path) -> None:
    a = Coverage(machine="M", states={"a"})
    a.merge(Coverage(machine="M", states={"b"}, guards={"g"}))
    assert a.states == {"a", "b"} and a.guards == {"g"}
    with pytest.raises(CommandError, match="not 'M'"):
        a.merge(Coverage(machine="Other"))
    path = tmp_path / "c.json"
    a.save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["states"] == ["a", "b"]
    assert Coverage.load(path) == a
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CommandError, match="cannot read coverage file"):
        Coverage.load(path)


def test_coverage_file_accumulates_across_commands(ctx: Context, tmp_path: Path) -> None:
    cov_file = tmp_path / "cov.json"
    machine, args = str(POWER_MODE), {"coverage_file": str(cov_file)}
    ctx.run("sm.simulate", {"machine": machine, "steps": "[start]", **args})
    first = {r["kind"]: r["covered"] for r in ctx.run("sm.coverage", {"machine": machine, "file": str(cov_file)})}
    assert first == {"states": 2, "transitions": 1, "guards": 0}
    ctx.run("sm.simulate", {"machine": machine, "steps": "[start, stop]", **args})  # start repeats, stop is new
    second = {r["kind"]: r["covered"] for r in ctx.run("sm.coverage", {"machine": machine, "file": str(cov_file)})}
    assert second == {"states": 2, "transitions": 2, "guards": 0}
    with pytest.raises(CommandError, match="not found"):
        ctx.run("sm.coverage", {"machine": machine, "file": str(tmp_path / "nope.json")})


# --- data generation ----------------------------------------------------------------------------------------


def test_value_pools_use_boundaries_around_constants() -> None:
    pools = value_pools(spec(invariants=['mode == "x" or level < 3']))
    assert set(pools) == {"level", "mode"}  # reserved names are never generated as data
    assert {9, 10, 11, 2, 3, 4, 0} <= set(pools["level"])
    assert pools["mode"] == ["x"]


def test_value_pools_bare_boolean_variable_gets_true_false() -> None:
    m = spec(transitions=[{"from": "a", "event": "go", "to": "b", "guard": "ok"}])
    assert value_pools(m) == {"ok": [True, False]}


def test_value_pools_are_bounded_by_packet_types() -> None:
    packet = StructManifest.model_validate({"name": "P", "fields": [{"name": "level", "type": "u8"}]})
    m = spec(transitions=[{"from": "a", "event": "go", "to": "b", "guard": "level > 255 or level < 0"}])
    pool = value_pools(m, packet)["level"]
    assert min(pool) == 0 and max(pool) == 255 and all(0 <= v <= 255 for v in pool)


def test_format_steps_matches_simulate_input() -> None:
    assert format_steps([("start", {}), ("overheat", {"temperature": 91})]) == "[start, {overheat: {temperature: 91}}]"


# --- fuzzing ------------------------------------------------------------------------------------------------


def test_fuzz_example_reaches_full_coverage_and_is_reproducible(ctx: Context) -> None:
    args = {"machine": str(POWER_MODE), "seed": 1, "runs": 200, "length": 30}
    rows = ctx.run("sm.fuzz", args)
    assert all(r["percent"] == 100.0 for r in rows)
    assert ctx.run("sm.fuzz", args) == rows


def test_fuzz_finds_invariant_violation_and_reproduction_works(ctx: Context, tmp_path: Path) -> None:
    # Bug: the guard threshold (100) is higher than the safety requirement (90) in the invariant.
    buggy = MachineSpec.load_from_file(POWER_MODE).model_dump(by_alias=True)
    buggy["transitions"][2]["guard"] = "temperature > 100"
    machine = tmp_path / "buggy.yaml"
    import yaml

    machine.write_text(yaml.safe_dump(buggy), encoding="utf-8")

    with pytest.raises(CommandError) as err:
        ctx.run("sm.fuzz", {"machine": str(machine), "seed": 7, "runs": 300, "length": 30})
    message = str(err.value)
    assert "seed 7" in message and "invariant violated" in message
    found = re.search(r'reproduce: sm.simulate \S+ "(.*)"$', message)
    assert found is not None
    steps = found.group(1)

    rows = ctx.run("sm.simulate", {"machine": str(machine), "steps": steps})
    last = rows[-1]
    assert last["event"] == "overheat" and last["from_state"] == "running" and last["to_state"] != "fault"


def test_fuzz_reports_evaluation_errors(ctx: Context, tmp_path: Path) -> None:
    import yaml

    broken = {
        "name": "B",
        "initial": "a",
        "states": [{"name": "a"}, {"name": "b"}],
        "events": [{"name": "go"}],
        "transitions": [{"from": "a", "event": "go", "to": "b", "guard": "10 / x > 1 and x > 0"}],
    }
    machine = tmp_path / "b.yaml"
    machine.write_text(yaml.safe_dump(broken), encoding="utf-8")
    with pytest.raises(CommandError, match=r"error: cannot evaluate '10 / x > 1 and x > 0'"):
        ctx.run("sm.fuzz", {"machine": str(machine), "seed": 3, "runs": 50, "length": 5})


def test_fuzz_still_saves_coverage_when_it_fails(ctx: Context, tmp_path: Path) -> None:
    import yaml

    broken = {
        "name": "B",
        "initial": "a",
        "states": [{"name": "a"}, {"name": "b"}],
        "events": [{"name": "go"}],
        "transitions": [{"from": "a", "event": "go", "to": "b"}],
        "invariants": ["state != 'b'"],
    }
    machine, cov = tmp_path / "b.yaml", tmp_path / "cov.json"
    machine.write_text(yaml.safe_dump(broken), encoding="utf-8")
    with pytest.raises(CommandError, match="invariant violated"):
        ctx.run("sm.fuzz", {"machine": str(machine), "seed": 1, "runs": 5, "length": 3, "coverage_file": str(cov)})
    assert Coverage.load(cov).states == {"a", "b"}


def test_invalid_invariant_rejected_by_schema() -> None:
    with pytest.raises(Exception, match="invalid expression"):
        spec(invariants=["x ==="])
