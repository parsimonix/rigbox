from __future__ import annotations

import io
import json
import warnings

import pytest
from pydantic import BaseModel
from rich.text import Text
from typer.testing import CliRunner

from rigbox.cli import cli
from rigbox.constants import RenderFormat
from rigbox.core.context import Context
from rigbox.core.registry import NoArgs, Registry
from rigbox.core.render import RenderError, RigboxCmdResult
from rigbox.plugins.statemachine.commands import rigbox_register
from rigbox.repl import RigboxShell


@pytest.mark.parametrize("fmt", [RenderFormat.RICH, RenderFormat.PLAINTEXT])
def test_human_formats_fall_back_to_text_and_tables(fmt: RenderFormat) -> None:
    stream = io.StringIO()
    assert RigboxCmdResult(text="Generated").render(fmt, stream) == "Generated"
    table = RigboxCmdResult(structured_data=[{"name": "[red]sensor"}, {"value": 2}]).render(fmt, stream)
    assert "name" in table and "value" in table
    assert "[red]sensor" in table and "2" in table


def test_human_formats_prefer_their_own_representation() -> None:
    result = RigboxCmdResult(text="plain", rich_data=Text("rich", style="red"), structured_data={"data": "row"})
    assert result.render(RenderFormat.PLAINTEXT, io.StringIO()) == "plain"
    assert result.render(RenderFormat.RICH, io.StringIO()).strip() == "rich"
    assert RigboxCmdResult(rich_data=Text("rich", style="red")).render(RenderFormat.PLAINTEXT).strip() == "rich"


@pytest.mark.parametrize("fmt", [RenderFormat.JSON, RenderFormat.NDJSON])
@pytest.mark.parametrize("result", [RigboxCmdResult(text="message"), RigboxCmdResult(rich_data=Text("message"))])
def test_machine_formats_reject_human_only_results(fmt: RenderFormat, result: RigboxCmdResult) -> None:
    with pytest.raises(RenderError, match="no structured data"):
        result.render(fmt, io.StringIO())


@pytest.mark.parametrize("data", [None, [], ()])
@pytest.mark.parametrize("fmt", list(RenderFormat))
def test_empty_results_are_warning_free(data: object, fmt: RenderFormat) -> None:
    with warnings.catch_warnings(record=True) as caught:
        output = RigboxCmdResult(structured_data=data).render(fmt, io.StringIO())
    assert output == ("[]" if fmt == RenderFormat.JSON else "")
    assert not caught


@pytest.mark.parametrize("fmt", [RenderFormat.JSON, RenderFormat.NDJSON])
def test_machine_formats_preserve_structured_results(fmt: RenderFormat) -> None:
    result = RigboxCmdResult(text="Generated", structured_data={"written": "output.h"})
    output = json.loads(result.render(fmt, io.StringIO()))
    assert output == ([{"written": "output.h"}] if fmt == RenderFormat.JSON else {"written": "output.h"})


def test_auto_uses_human_fallback_on_terminal_and_strict_ndjson_when_piped() -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    result = RigboxCmdResult(text="message")
    assert "message" in result.render(RenderFormat.AUTO, Terminal())
    with pytest.raises(RenderError):
        result.render(RenderFormat.AUTO, io.StringIO())


@pytest.mark.parametrize("tokens", [["run", "--format", "json", "text_only"], ["script", "workflow.yaml", "--format", "json"]])
def test_cli_reports_unavailable_machine_output_on_stderr(monkeypatch: pytest.MonkeyPatch, tokens: list[str]) -> None:
    registry = Registry()

    class WorkflowArgs(BaseModel):
        workflow: str
        var: list[str]

    @registry.command("workflow.run", args=WorkflowArgs, record=False)
    def workflow(ctx: Context, args: WorkflowArgs) -> str:
        """Return a human-only workflow message."""
        return "message"

    @registry.command("text_only", record=False)
    def text_only(ctx: Context, args: NoArgs) -> str:
        """Return a human-only message."""
        return "message"

    def build_context(workspace: str | None, fmt: RenderFormat) -> Context:
        return Context(registry=registry, fmt=fmt)

    monkeypatch.setattr("rigbox.cli._build_context", build_context)
    result = CliRunner().invoke(cli, tokens)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "no structured data" in result.stderr


def test_shell_reports_render_errors_and_remains_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Registry()

    @registry.command("text_only", record=False)
    def text_only(ctx: Context, args: NoArgs) -> str:
        """Return a human-only message."""
        return "message"

    output = io.StringIO()
    shell = RigboxShell(Context(registry=registry), stdout=output)
    errors: list[str] = []
    monkeypatch.setattr(shell, "perror", errors.append)
    monkeypatch.setattr(shell, "format", RenderFormat.JSON)
    shell.run_command("text_only", {})
    assert output.getvalue() == ""
    assert len(errors) == 1 and "no structured data" in errors[0]
    monkeypatch.setattr(shell, "format", RenderFormat.RICH)
    shell.run_command("text_only", {})
    assert "message" in output.getvalue()


def test_sm_check_cli_has_one_final_newline() -> None:
    result = CliRunner().invoke(cli, ["run", "--format", "rich", "sm.check", "examples\\power_mode.yaml"])
    assert result.exit_code == 0
    assert result.stdout == "No structural issues found\n"


def test_sm_check_shell_has_one_final_newline() -> None:
    registry = Registry()
    rigbox_register(registry)
    output = io.StringIO()
    shell = RigboxShell(Context(registry=registry), stdout=output)
    shell.run_command("sm.check", {"machine": "examples\\power_mode.yaml"})
    assert output.getvalue() == "No structural issues found\n"


@pytest.mark.parametrize("fmt", [RenderFormat.PLAINTEXT, RenderFormat.RICH])
def test_rich_rendering_removes_only_console_added_newline(fmt: RenderFormat) -> None:
    result = RigboxCmdResult(rich_data=Text("first\nsecond\n"))
    assert result.render(fmt, io.StringIO()) == "first\nsecond\n"
