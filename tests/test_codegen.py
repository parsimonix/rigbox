from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import StrictUndefined
from jinja2 import Template as JinjaTemplate

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.plugins.code.commands import rigbox_register
from rigbox.plugins.code.generator import CodeGenerator
from rigbox.plugins.code.render import CStateMachineContext
from rigbox.plugins.code.template import Template, get_builtin_template
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

    assert "code.state_machine" in registry.names()


@pytest.mark.parametrize(
    ("manifest", "template", "expected"),
    [
        (
            {"project": {"name": "sensor"}, "pins": [2, 5], "enabled": True},
            "{{ context.project.name }}: {{ context.pins | join(',') }} / {{ context.enabled }}",
            "sensor: 2,5 / True",
        ),
        (["alpha", "beta"], "{{ context | join(',') }}", "alpha,beta"),
        (42, "{{ context }}", "42"),
        (None, "{{ context is none }}", "True"),
    ],
)
def test_generate_external_template_receives_raw_manifest(tmp_path: Path, manifest: object, template: str, expected: str) -> None:
    source = tmp_path / "manifest.yaml"
    source.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    template_path = tmp_path / "custom.txt.j2"
    template_path.write_text(template, encoding="utf-8")
    output = tmp_path / "generated.txt"
    registry = Registry()
    rigbox_register(registry)
    ctx = Context(registry=registry)

    result = ctx.run("code.generate", {"template": str(template_path), "manifest": str(source), "out": str(output)})

    assert output.read_text(encoding="utf-8") == expected
    assert result.structured_data == {"written": str(output)}
    assert result.text == f"Generated code written to '{output}'"


@pytest.mark.parametrize(
    ("updates", "template", "error"),
    [
        ({}, "{{ context.states[0].symbol }} = {{ context.states[0].value }}", None),
        ({"version": None}, "", "version: Input should be a valid string"),
        ({"initial": "missing"}, "", "initial state 'missing' is not defined"),
        ({"states": [{"name": "idle"}, {"name": "Idle"}, {"name": "running"}]}, "", "collide"),
        ({}, "{{ context.missing }}", "cannot render code-generation template"),
    ],
)
def test_generate_builtin_validates_manifest_and_preserves_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, updates: dict[str, object], template: str, error: str | None
) -> None:
    def load_test_template(self: CodeGenerator, definition: Template[MachineSpec, CStateMachineContext]) -> JinjaTemplate:
        return JinjaTemplate(template, undefined=StrictUndefined)

    monkeypatch.setattr(CodeGenerator, "load_builtin_template", load_test_template)
    data: dict[str, object] = {
        "name": "Power Mode",
        "version": "1.0",
        "initial": "idle",
        "states": [{"name": "idle"}, {"name": "running"}],
        "events": [{"name": "start"}],
        "transitions": [{"from": "idle", "event": "start", "to": "running"}],
    }
    data.update(updates)
    source = tmp_path / "machine.yaml"
    source.write_text(yaml.safe_dump(data), encoding="utf-8")
    output = tmp_path / "generated" / "machine.h"
    registry = Registry()
    rigbox_register(registry)
    ctx = Context(registry=registry)
    args = {"template": "c-state-machine-header", "manifest": str(source), "out": str(output)}

    if error is not None:
        with pytest.raises(CommandError, match=error):
            ctx.run("code.generate", args)
        assert not output.parent.exists()
    else:
        ctx.run("code.generate", args)
        generated = output.read_text(encoding="utf-8")
        assert generated == "power_mode_STATE_IDLE = 0"
        with pytest.raises(CommandError, match="output already exists"):
            ctx.run("code.generate", args)
        assert output.read_text(encoding="utf-8") == generated
        output.write_text("stale", encoding="utf-8")
        ctx.run("code.generate", {**args, "force": True})
        assert output.read_text(encoding="utf-8") == generated


@pytest.mark.parametrize("manifest_text", [None, "project: ["])
def test_generate_reports_manifest_read_errors(tmp_path: Path, manifest_text: str | None) -> None:
    source = tmp_path / "manifest.yaml"
    if manifest_text is not None:
        source.write_text(manifest_text, encoding="utf-8")
    template_path = tmp_path / "custom.txt.j2"
    template_path.write_text("{{ context }}", encoding="utf-8")
    output = tmp_path / "generated" / "output.txt"
    registry = Registry()
    rigbox_register(registry)

    with pytest.raises(CommandError, match="cannot read manifest"):
        Context(registry=registry).run(
            "code.generate", {"template": str(template_path), "manifest": str(source), "out": str(output)}
        )

    assert not output.parent.exists()


def test_codegen_command_writes_header_and_requires_force_to_replace(tmp_path: Path) -> None:
    source = tmp_path / "power.yaml"
    source.write_text(yaml.safe_dump(_machine().model_dump(mode="json", by_alias=True)), encoding="utf-8")
    output = tmp_path / "generated" / "power.h"
    registry = Registry()
    rigbox_register(registry)
    ctx = Context(registry=registry)

    result = ctx.run("code.state_machine", {"machine": str(source), "out": str(output)})
    generated = output.read_text(encoding="utf-8")
    assert result == [{"written": str(output)}]
    assert "RIGBOX_POWER_MODE_STATE_IDLE = 0" in generated

    with pytest.raises(CommandError, match="cannot write generated header"):
        ctx.run("code.state_machine", {"machine": str(source), "out": str(output)})
    assert output.read_text(encoding="utf-8") == generated

    output.write_text("stale", encoding="utf-8")
    ctx.run("code.state_machine", {"machine": str(source), "out": str(output), "force": True})
    assert "RIGBOX_POWER_MODE_EVENT_START = 0" in output.read_text(encoding="utf-8")
