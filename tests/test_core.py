import io
import json
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from rigbox import cli as cli_module
from rigbox.cli import cli
from rigbox.core.cli_parser import split_line
from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.render import HumanReadableResponse, RigboxResponse, normalize_human_readable, normalize_structured_data, render
from rigbox.core.workflow import Step, Workflow, run_workflow, substitute
from rigbox.core.workspace import Workspace


class _TerminalStream(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def ctx() -> Context:
    reg = Registry()
    load_all(reg)
    return Context(registry=reg)


def test_split_line_keeps_windows_paths_and_quotes() -> None:
    assert split_line(r'load "C:\a b\x.yaml" --n 2') == ["load", r"C:\a b\x.yaml", "--n", "2"]


def test_unknown_command_suggests(ctx: Context) -> None:
    with pytest.raises(CommandError, match="did you mean"):
        ctx.registry.get("demo.ech")


def test_commands_lists_every_command_or_those_with_a_prefix(ctx: Context) -> None:
    listed = {row["command"] for row in normalize_structured_data(ctx.run_line("commands").structured_data)}
    assert {"commands", "device.connect", "sm.fuzz"} <= listed
    assert "help" not in listed and "history" not in listed  # the shell provides these itself
    only_sm = normalize_structured_data(ctx.run_line("commands --prefix sm.").structured_data)
    assert only_sm and all(row["command"].startswith("sm.") for row in only_sm)
    assert all(row["summary"] for row in only_sm)


def test_run_line_validates_and_returns_rows(ctx: Context) -> None:
    rows = ctx.run_line("demo.echo hi --times 2 --upper")
    assert rows.structured_data == [{"i": 1, "message": "HI"}, {"i": 2, "message": "HI"}]
    with pytest.raises(CommandError, match="invalid arguments"):
        ctx.run_line("demo.echo hi --times 0")


def test_history_records_only_recordable_commands(ctx: Context) -> None:
    ctx.run_line("demo.echo a")
    ctx.run_line("commands")
    assert [e["run"] for e in ctx.history.entries()] == ["demo.echo"]


def test_render_formats() -> None:
    rows = [{"a": 1, "b": "x"}, {"a": 22, "b": "y"}]
    response = RigboxResponse(rows)
    assert json.loads(render(response, "json")) == rows
    assert [json.loads(line) for line in render(response, "ndjson").splitlines()] == rows
    assert render(response, "table").splitlines()[0].split() == ["a", "b"]
    assert render(RigboxResponse(), "json") == "[]"


@pytest.mark.parametrize(
    "level, ansi",
    [("critical", "\x1b[1;31m"), ("error", "\x1b[31m"), ("warning", "\x1b[33m"), ("success", "\x1b[32m")],
)
def test_human_message_severity_colors(level: Literal["critical", "error", "warning", "success"], ansi: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    message = HumanReadableResponse("[literal] message", level)
    output = normalize_human_readable(message, _TerminalStream())
    assert ansi in output
    assert "[literal] message" in output


def test_human_message_respects_plain_output_and_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    message = HumanReadableResponse(" message ", "success")
    assert normalize_human_readable(message, io.StringIO()) == "message"
    assert normalize_human_readable(HumanReadableResponse("raw", "raw"), _TerminalStream()) == "raw"
    monkeypatch.setenv("NO_COLOR", "1")
    assert normalize_human_readable(message, _TerminalStream()) == "message"


def test_human_message_without_structured_data() -> None:
    response = RigboxResponse(None, HumanReadableResponse("No issues found", "success"))
    assert render(response, "human", io.StringIO()) == "No issues found"
    assert render(response, "json", io.StringIO()) == "[]"


def test_render_colors_level_rows_only_for_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"level": "warning", "message": "careful"},
        {"level": "error", "message": "failed"},
        {"level": "success", "message": "done"},
    ]
    terminal = _TerminalStream()

    response = RigboxResponse(rows)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert "\x1b[" in render(response, "auto", terminal)
    assert "\x1b[" not in render(response, "table", io.StringIO())

    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b[" not in render(response, "table", terminal)


def test_render_keeps_json_and_ndjson_uncolored_on_terminal() -> None:
    rows = [{"level": "error", "message": "failed"}]
    terminal = _TerminalStream()

    assert "\x1b[" not in render(RigboxResponse(rows), "json", terminal)
    assert "\x1b[" not in render(RigboxResponse(rows), "ndjson", terminal)


def test_explicit_response_survives_dispatch_and_workflow() -> None:
    registry = Registry()
    message = HumanReadableResponse("Ready", "success")

    @registry.command("test.response")
    def response_handler(ctx: Context, args: object) -> RigboxResponse:
        return RigboxResponse({"ready": True}, message)

    ctx = Context(registry)
    workflow = Workflow(name="test", steps=[Step(run="test.response")])
    response = run_workflow(ctx, workflow)
    assert response.structured_data == [{"ready": True}]
    assert response.human_readable == message
    assert render(response, "human", io.StringIO()) == "Ready"
    assert json.loads(render(response, "json")) == [{"ready": True}]


def test_message_only_and_empty_commands() -> None:
    registry = Registry()

    @registry.command("test.message", record=False)
    def message_handler(ctx: Context, args: object) -> HumanReadableResponse:
        return HumanReadableResponse("Done", "success")

    ctx = Context(registry)
    response = ctx.run_line("test.message")
    assert response.human_readable == HumanReadableResponse("Done", "success")
    assert render(response, "json") == "[]"
    assert render(ctx.run_tokens([]), "human") == ""


def test_cli_selects_response_representation(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Registry()

    @registry.command("test.response", record=False)
    def response_handler(ctx: Context, args: object) -> RigboxResponse:
        return RigboxResponse({"ready": True}, HumanReadableResponse("Ready", "success"))

    @registry.command("test.message", record=False)
    def message_handler(ctx: Context, args: object) -> HumanReadableResponse:
        return HumanReadableResponse("Done", "info")

    def build_context(workspace: str | None, fmt: str) -> Context:
        return Context(registry, fmt=fmt)

    monkeypatch.setattr(cli_module, "_build_context", build_context)
    runner = CliRunner()
    human = runner.invoke(cli, ["run", "--format", "human", "test.response"])
    assert human.exit_code == 0, human.output
    assert human.stdout.strip() == "Ready"
    structured = runner.invoke(cli, ["run", "--format", "json", "test.response"])
    assert structured.exit_code == 0, structured.output
    assert json.loads(structured.stdout) == [{"ready": True}]
    message = runner.invoke(cli, ["run", "--format", "human", "test.message"])
    assert message.exit_code == 0, message.output
    assert message.stdout.strip() == "Done"


def test_substitute_preserves_type_for_whole_values() -> None:
    assert substitute({"n": "${x}", "s": "v=${x}"}, {"x": 5}) == {"n": 5, "s": "v=5"}
    with pytest.raises(CommandError):
        substitute("${missing}", {})


def test_workspace_init_and_find(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj", "p")
    assert (ws.root / "rigbox.yaml").is_file() and ws.workflows_dir.is_dir()
    found = Workspace.find(ws.root / "plugins")
    assert found is not None and found.root == ws.root
    with pytest.raises(FileExistsError):
        Workspace.init(ws.root)


def test_workspace_plugin_loading_and_failure_isolation(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj")
    (ws.plugins_dir / "hello.py").write_text(
        "from rigbox.hooks import hookimpl\n"
        "@hookimpl\n"
        "def rigbox_register(registry):\n"
        "    @registry.command('hello.say')\n"
        "    def say(ctx, args):\n"
        "        return {'msg': 'hi'}\n",
        encoding="utf-8",
    )
    (ws.plugins_dir / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    (ws.plugins_dir / "nohook.py").write_text("X = 1\n", encoding="utf-8")
    reg = Registry()
    load_all(reg, ws)
    ctx = Context(registry=reg, workspace=ws)
    assert ctx.run("hello.say").structured_data == [{"msg": "hi"}]
    by_name = {p.name: p for p in reg.plugins}
    assert by_name["hello"].commands == ["hello.say"]
    broken, nohook = by_name["broken"].error, by_name["nohook"].error
    assert broken is not None and "boom" in broken
    assert nohook is not None and "no rigbox_register hook" in nohook


def test_history_to_workflow_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = Workspace.init(tmp_path / "proj")
    reg = Registry()
    load_all(reg, ws)
    ctx = Context(registry=reg, workspace=ws)
    ctx.run_line("demo.echo one")
    ctx.run_line("demo.echo two --times 2")
    ctx.run_line("workflow.save twice --last 2")
    assert (ws.workflows_dir / "twice.yaml").is_file()
    rows = ctx.run_line("workflow.run twice")
    assert [r["message"] for r in normalize_structured_data(rows.structured_data)] == ["two", "two"]
    # history persisted to disk for the next session
    assert (ws.data_dir / "history.jsonl").is_file()


def test_cli_run_and_script_modes(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj")
    wf = ws.workflows_dir / "greet.yaml"
    wf.write_text(
        "name: greet\nvars: {who: world}\nsteps:\n  - run: demo.echo\n    args: {message: 'hi ${who}'}\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--workspace", str(ws.root), "--format", "json", "demo.echo", "x"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"i": 1, "message": "x"}]

    result = runner.invoke(cli, ["script", str(wf), "--workspace", str(ws.root), "--format", "ndjson", "--var", "who=you"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"i": 1, "message": "hi you"}

    result = runner.invoke(cli, ["run", "--workspace", str(ws.root), "nope"])
    assert result.exit_code == 1
    assert "unknown command" in result.stderr
    assert result.stdout == ""


def test_cli_human_format_and_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--format", "human", "demo.echo", "hi"])
    assert result.exit_code == 0, result.output
    assert "hi" in result.stdout
    result = runner.invoke(cli, ["run", "demo.echo", "--help"])
    assert result.exit_code == 0
