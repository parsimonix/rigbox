import io
from pathlib import Path
from typing import Literal, TextIO

import pytest
from pydantic import BaseModel, Field
from rich.text import Text
from typer.testing import CliRunner

from rigbox import cli as cli_module
from rigbox.constants import RenderFormat
from rigbox.core.cli_parser import build_parser, parse_args, parser_for, path_field
from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import NoArgs, Registry, UsageError
from rigbox.core.render import RigboxCmdResult
from rigbox.repl import build_shell


class Args(BaseModel):
    """Arguments used to exercise the parser."""

    name: str = Field(description="Who to greet")
    count: int = Field(1, description="How many times")
    loud: bool = False
    tag: list[str] = Field(default_factory=list, description="A label, repeatable")
    mode: Literal["fast", "slow"] | None = Field(None, description="Speed")
    where: str = path_field("here", description="A file")
    ratio: float = Field(0.5, description="Fill 50% of it")


PARSER = build_parser("greet", Args, "Say hello.")


class WorkflowInvocationArgs(BaseModel):
    """Accept the arguments forwarded by the script CLI."""

    workflow: str
    var: list[str] = Field(default_factory=list)


@pytest.mark.parametrize("action", ["run", "script"])
@pytest.mark.parametrize("fmt", [None, *RenderFormat])
def test_cli_format_values_are_enums(action: str, fmt: RenderFormat | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[RenderFormat] = []
    rendered: list[RenderFormat] = []
    registry = Registry()

    @registry.command("test.format", record=False)
    def run_handler(ctx: Context, args: NoArgs) -> RigboxCmdResult:
        assert isinstance(ctx.fmt, RenderFormat)
        return RigboxCmdResult(text="Ready", structured_data={"ready": True}, rich_data=Text("Ready"))

    @registry.command("workflow.run", args=WorkflowInvocationArgs, record=False)
    def script_handler(ctx: Context, args: WorkflowInvocationArgs) -> RigboxCmdResult:
        assert isinstance(ctx.fmt, RenderFormat)
        return RigboxCmdResult(text="Ready", structured_data={"ready": True}, rich_data=Text("Ready"))

    def build_context(workspace: str | None, fmt: RenderFormat) -> Context:
        assert isinstance(fmt, RenderFormat)
        seen.append(fmt)
        return Context(registry=registry, fmt=fmt)

    original_render = RigboxCmdResult.render

    def record_render(self: RigboxCmdResult, fmt: RenderFormat, stream: TextIO | None = None) -> str:
        assert isinstance(fmt, RenderFormat)
        rendered.append(fmt)
        return original_render(self, fmt, stream)

    monkeypatch.setattr(cli_module, "_build_context", build_context)
    monkeypatch.setattr(RigboxCmdResult, "render", record_render)
    options = ["--format", fmt.value] if fmt is not None else []
    args = ["run", *options, "test.format"] if action == "run" else ["script", str(tmp_path / "workflow.yaml"), *options]
    result = CliRunner().invoke(cli_module.cli, args)

    assert result.exit_code == 0, result.output
    assert seen == [fmt if fmt is not None else RenderFormat.default()]
    assert rendered == seen


def test_cli_startup_uses_enum_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from rigbox import repl

    seen: list[RenderFormat] = []

    def build_context(workspace: str | None, fmt: RenderFormat) -> Context:
        assert isinstance(fmt, RenderFormat)
        seen.append(fmt)
        return Context(Registry(), fmt=fmt)

    def run_repl(ctx: Context) -> int:
        assert isinstance(ctx.fmt, RenderFormat)
        return 0

    monkeypatch.setattr(cli_module, "_build_context", build_context)
    monkeypatch.setattr(repl, "run_repl", run_repl)
    result = CliRunner().invoke(cli_module.cli, [])
    assert result.exit_code == 0, result.output
    assert seen == [RenderFormat.default()]


@pytest.mark.parametrize("action", ["run", "script"])
def test_cli_rejects_invalid_format(action: str, tmp_path: Path) -> None:
    args = ["run", "--format", "invalid", "test.format"] if action == "run" else ["script", str(tmp_path / "workflow.yaml"), "--format", "invalid"]
    result = CliRunner().invoke(cli_module.cli, args)
    assert result.exit_code == 2
    assert "invalid" in result.output


def test_shell_format_setting_keeps_enum_values() -> None:
    registry = Registry()
    seen: list[RenderFormat] = []

    @registry.command("test.format", record=False)
    def handler(ctx: Context, args: NoArgs) -> RigboxCmdResult:
        assert isinstance(ctx.fmt, RenderFormat)
        seen.append(ctx.fmt)
        return RigboxCmdResult(text="Ready", structured_data={"ready": True}, rich_data=Text("Ready"))

    shell = build_shell(Context(registry), stdout=io.StringIO())
    for fmt in RenderFormat:
        shell.onecmd_plus_hooks(f"set format {fmt.value}")
        assert isinstance(shell.format, RenderFormat)
        assert shell.format is fmt
        shell.onecmd_plus_hooks("test.format")
        assert shell.ctx.fmt is fmt
    assert seen == list(RenderFormat)
    shell.onecmd_plus_hooks("set format invalid")
    assert shell.format is RenderFormat.AUTO


@pytest.fixture
def ctx() -> Context:
    reg = Registry()
    load_all(reg)
    return Context(registry=reg)


def test_positional_options_flags_and_lists_become_a_raw_dict_of_what_was_given() -> None:
    raw = parse_args(PARSER, ["bob", "--count=3", "--loud", "--tag", "a", "--tag", "b", "--mode", "fast"])
    assert raw == {"name": "bob", "count": "3", "loud": True, "tag": ["a", "b"], "mode": "fast"}
    assert parse_args(PARSER, ["bob"]) == {"name": "bob"}  # defaults are left to pydantic


def test_optional_positional_accepts_intermixed_options_without_changing_defaults() -> None:
    class FrameArgs(BaseModel):
        name: str
        protocol: str = "raw"
        frame: str | None = Field(None, json_schema_extra={"positional": True})

    parser = build_parser("request", FrameArgs, "Send a request.")
    assert parse_args(parser, ["dut", "--protocol", "race", "05 5a"]) == {
        "name": "dut",
        "protocol": "race",
        "frame": "05 5a",
    }
    assert parse_args(parser, ["dut"]) == {"name": "dut"}


def test_negative_numbers_and_values_with_spaces_are_values_not_options() -> None:
    assert parse_args(PARSER, ["bob", "--count", "-3", "--where", "C:\\a b\\x.yaml"]) == {
        "name": "bob",
        "count": "-3",
        "where": "C:\\a b\\x.yaml",
    }


@pytest.mark.parametrize(
    ("tokens", "fragment"),
    [
        (["bob", "--nope", "x"], "unrecognized arguments: --nope x"),
        (["bob", "extra"], "unrecognized arguments: extra"),
        (["bob", "--count"], "expected one argument"),
        ([], "the following arguments are required: name"),
        (["bob", "--mode", "medium"], "invalid choice: 'medium'"),
        (["bob", "--cou", "3"], "unrecognized arguments"),  # no abbreviations: options mean exactly what they say
    ],
)
def test_bad_command_lines_raise_a_usage_error_with_the_parsers_message(tokens: list[str], fragment: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(UsageError, match=fragment) as caught:
        parse_args(PARSER, tokens)
    assert caught.value.exit_code == 2
    assert "Usage: greet" in capsys.readouterr().err  # the parser already told the user


def test_help_is_printed_and_reported_as_a_usage_error_with_exit_code_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(UsageError) as caught:
        parse_args(PARSER, ["bob", "--help"])
    assert caught.value.exit_code == 0
    out = capsys.readouterr().out
    assert "Usage: greet" in out and "Say hello." in out
    assert "Who to greet" in out and "How many times (default: 1)" in out
    assert "Fill 50% of it (default: 0.5)" in out  # a % in help text survives argparse's formatting
    assert "--loud" in out and "{fast,slow}" in out


def test_parsers_are_built_once_per_command(ctx: Context) -> None:
    command = ctx.registry.get("demo.echo")
    assert parser_for(command) is parser_for(command)
    assert parser_for(command).fields == ("message", "times", "upper")


def test_run_tokens_uses_the_same_parser(ctx: Context, capsys: pytest.CaptureFixture[str]) -> None:
    assert ctx.run_tokens(["demo.echo", "hi", "--times", "2"]) == [
        {"i": 1, "message": "hi"},
        {"i": 2, "message": "hi"},
    ]
    with pytest.raises(UsageError, match="unrecognized arguments: --bogus"):
        ctx.run_tokens(["demo.echo", "hi", "--bogus"])
    with pytest.raises(UsageError) as caught:
        ctx.run_tokens(["dsp.gen", "--help"])
    assert caught.value.exit_code == 0
    assert "Generate a test signal" in capsys.readouterr().out


def test_cli_exit_codes_for_help_bad_usage_and_failures() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_module.cli, ["run", "demo.echo", "--help"])
    assert result.exit_code == 0
    assert "Usage: demo.echo" in result.stdout
    result = runner.invoke(cli_module.cli, ["run", "demo.echo", "hi", "--bogus"])
    assert result.exit_code == 2
    assert "unrecognized arguments: --bogus" in result.stderr
    result = runner.invoke(cli_module.cli, ["run", "demo.echo", "hi", "--times", "0"])
    assert result.exit_code == 1
    assert "invalid arguments" in result.stderr
    result = runner.invoke(cli_module.cli, ["run", "nosuchcommand"])
    assert result.exit_code == 1
    assert "unknown command" in result.stderr
