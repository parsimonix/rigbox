from typing import Literal

import pytest
from pydantic import BaseModel, Field

from rigbox.cli import main
from rigbox.core.cli_parser import build_parser, parse_args, parser_for, path_field
from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import Registry, UsageError


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


@pytest.fixture
def ctx() -> Context:
    reg = Registry()
    load_all(reg)
    return Context(registry=reg)


def test_positional_options_flags_and_lists_become_a_raw_dict_of_what_was_given() -> None:
    raw = parse_args(PARSER, ["bob", "--count=3", "--loud", "--tag", "a", "--tag", "b", "--mode", "fast"])
    assert raw == {"name": "bob", "count": "3", "loud": True, "tag": ["a", "b"], "mode": "fast"}
    assert parse_args(PARSER, ["bob"]) == {"name": "bob"}  # defaults are left to pydantic


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
def test_bad_command_lines_raise_a_usage_error_with_the_parsers_message(
    tokens: list[str], fragment: str, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_cli_exit_codes_for_help_bad_usage_and_failures(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "dsp.gen", "--help"]) == 0
    assert "Usage: dsp.gen" in capsys.readouterr().out
    assert main(["run", "demo.echo", "hi", "--bogus"]) == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err
    assert main(["run", "demo.echo", "hi", "--times", "0"]) == 1  # parses fine, then fails validation
    assert "invalid arguments" in capsys.readouterr().err
    assert main(["run", "nosuchcommand"]) == 1
    assert "unknown command" in capsys.readouterr().err
