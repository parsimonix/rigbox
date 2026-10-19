from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.plugins.codegen.commands import rigbox_register
from rigbox.plugins.codegen.generator import CodeGenerator
from rigbox.plugins.codegen.template import get_builtin_template
from rigbox.plugins.statemachine.schema import MachineSpec


def _machine(name: str = "Power Mode") -> MachineSpec:
    return MachineSpec.model_validate(
        {
            "name": name,
            "initial": "idle",
            "states": [{"name": "idle"}, {"name": "running"}],
            "events": [{"name": "start"}, {"name": "stop"}],
            "transitions": [
                {"from": "idle", "event": "start", "to": "running"},
                {"from": "running", "event": "stop", "to": "idle"},
            ],
        }
    )


def test_c_header_renders_state_and_event_enums() -> None:
    text = CodeGenerator().render(get_builtin_template("c-state-machine-header"), _machine())

    assert "#ifndef RIGBOX_POWER_MODE_STATE_MACHINE_H" in text
    assert "RIGBOX_POWER_MODE_STATE_IDLE = 0" in text
    assert "RIGBOX_POWER_MODE_STATE_RUNNING = 1" in text
    assert "RIGBOX_POWER_MODE_EVENT_START = 0" in text
    assert "RIGBOX_POWER_MODE_EVENT_STOP = 1" in text
    assert "RIGBOX_POWER_MODE_EVENT_NULL" in text
    assert "rigbox_power_mode_state_t" in text


def test_c_context_rejects_names_that_collide_after_normalization() -> None:
    machine = _machine()
    machine.states.append(machine.states[0].model_copy(update={"name": "Idle"}))

    with pytest.raises(CommandError, match="collide"):
        CodeGenerator().render(get_builtin_template("c-state-machine-header"), machine)


def test_codegen_command_is_loaded_as_a_builtin() -> None:
    registry = Registry()
    load_all(registry)

    assert "codegen.state_machine" in registry.names()


def test_codegen_command_writes_header_and_requires_force_to_replace(tmp_path: Path) -> None:
    source = tmp_path / "power.yaml"
    source.write_text(yaml.safe_dump(_machine().model_dump(mode="json", by_alias=True)), encoding="utf-8")
    output = tmp_path / "generated" / "power.h"
    registry = Registry()
    rigbox_register(registry)
    ctx = Context(registry=registry)

    result = ctx.run("codegen.state_machine", {"machine": str(source), "out": str(output)})
    generated = output.read_text(encoding="utf-8")
    assert result == [{"written": str(output)}]
    assert "RIGBOX_POWER_MODE_STATE_IDLE = 0" in generated

    with pytest.raises(CommandError, match="cannot write generated header"):
        ctx.run("codegen.state_machine", {"machine": str(source), "out": str(output)})
    assert output.read_text(encoding="utf-8") == generated

    output.write_text("stale", encoding="utf-8")
    ctx.run("codegen.state_machine", {"machine": str(source), "out": str(output), "force": True})
    assert "RIGBOX_POWER_MODE_EVENT_START = 0" in output.read_text(encoding="utf-8")
