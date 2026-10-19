"""Module for command-line parsing using pydantic models.

Rigbox defines all commands using pydantic models to represent their arguments.

This module provides functions to convert these pydantic models into argparse parsers,
so that commands can be run both from the command line and the interactive shell with consistent argument handling.

Required fields in the pydantic model become positional arguments, while optional fields become named options with `--`.
Boolean fields become bare flags, list fields can repeat, and Literal fields become validated choices.

Values stay as strings when parsed from the command line, and pydantic handles conversion and validation when the command runs.
"""

from __future__ import annotations

import argparse
import functools
import shlex
import typing
from types import UnionType
from typing import Any, NoReturn

import cmd2
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from rigbox.core.registry import Command, UsageError


def path_field(default: Any = ..., **kwargs: Any) -> Any:  # noqa: ANN401 - mirrors pydantic's Field()
    """Declare a ``str`` argument that holds a file path, so the shell can tab-complete file names for it.

    Takes the same arguments as pydantic's ``Field``; with no default the argument is required.
    """
    return Field(default, json_schema_extra={"path": True}, **kwargs)


class RigboxParser(cmd2.Cmd2ArgumentParser):
    """The parser for one command. It remembers the last error so non-interactive callers can report it."""

    fields: tuple[str, ...] = ()
    """Names of the args model's fields, used to pick them out of the parsed namespace."""

    error_message: str = ""

    def error(self, message: str) -> NoReturn:
        """Record `message`, then let argparse print the usage and exit as usual."""
        self.error_message = message
        super().error(message)


def _unwrap(annotation: object) -> object:
    """Strip Optional[...] so `bool | None` still counts as bool."""
    if typing.get_origin(annotation) in (typing.Union, UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_bool(info: FieldInfo) -> bool:
    return _unwrap(info.annotation) is bool


def _is_list(info: FieldInfo) -> bool:
    return typing.get_origin(_unwrap(info.annotation)) in (list, tuple, set)


def _is_path(info: FieldInfo) -> bool:
    extra = info.json_schema_extra
    return isinstance(extra, dict) and extra.get("path") is True


def _help_text(info: FieldInfo) -> str:
    """The field's description, with its default appended; `%` is doubled because argparse formats help text."""
    text = info.description or ""
    if info.default_factory is None and info.default not in (None, "", False) and not info.is_required():
        text = f"{text} (default: {info.default})".strip()
    return text.replace("%", "%%")


def build_parser(name: str, model: type[BaseModel], summary: str) -> RigboxParser:
    """Build the parser for command `name` from its args `model`; `summary` is shown as the description."""
    # First, create a parser instance
    parser = RigboxParser(prog=name, description=summary, allow_abbrev=False, suggest_on_error=True)
    # Then, create the fields using the pydantic model's field definitions
    parser.fields = tuple(model.model_fields)
    # Now, we add each field as an argument to the parser based on its properties
    for field, info in model.model_fields.items():
        annotation = _unwrap(info.annotation)
        options: dict[str, Any] = {"help": _help_text(info)}
        if typing.get_origin(annotation) is typing.Literal:
            options["choices"] = list(typing.get_args(annotation))
        # Automatically set up path completion for path fields
        if _is_path(info):
            options["completer"] = cmd2.Cmd.path_complete
        if info.is_required():
            parser.add_argument(field, **options)
            continue
        flag = "--" + field.replace("_", "-")
        if _is_bool(info):
            parser.add_argument(flag, dest=field, action="store_true", default=argparse.SUPPRESS, help=options["help"])
        elif _is_list(info):
            parser.add_argument(flag, dest=field, action="append", default=argparse.SUPPRESS, **options)
        else:
            parser.add_argument(flag, dest=field, default=argparse.SUPPRESS, **options)
    return parser


@functools.cache  # Cache the parser for each command to avoid rebuilding it multiple times
def parser_for(command: Command) -> RigboxParser:
    """Return the (cached) parser for a registered command."""
    return build_parser(command.name, command.args_model, command.help)


def parse_args(parser: RigboxParser, tokens: list[str]) -> dict[str, Any]:
    """Parse command-line `tokens` into a raw args dict holding only the options actually given.

    Raises:
        UsageError: If the tokens are invalid, or ``--help`` was given. The parser has already printed the usage
            message or help text; the error's ``exit_code`` is 2 for a bad command line and 0 for help.
    """
    try:
        namespace = parser.parse_args(tokens)
    except SystemExit as exit_:
        code = exit_.code
        raise UsageError(parser.error_message, code if isinstance(code, int) else 0) from None
    return {key: value for key, value in vars(namespace).items() if key in parser.fields}


def split_line(line: str) -> list[str]:
    """Split a line into tokens. Quotes group words; backslashes are kept (Windows paths)."""
    lexer = shlex.shlex(line, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens: list[str] = []
    for tok in lexer:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        tokens.append(tok)
    return tokens
