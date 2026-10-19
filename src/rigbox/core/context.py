"""Execution context shared by REPL, CLI and workflows."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO

from pydantic import ValidationError
from rich.console import Console, RenderableType

from rigbox.constants import RenderFormat
from rigbox.core.cli_parser import parse_args, parser_for, split_line
from rigbox.core.history import History
from rigbox.core.registry import CommandError, Registry
from rigbox.core.render import RigboxCmdResult, resolve_render_format
from rigbox.core.workspace import Workspace


class Closable(Protocol):
    """Anything with a ``close()`` method, such as a device connection."""

    def close(self) -> None:
        """Release the resource."""


@dataclass
class Context:
    """Shared state for one session: registry, workspace, history, output format and open connections."""

    registry: Registry
    workspace: Workspace | None = None
    fmt: RenderFormat = RenderFormat.default()
    history: History = field(init=False)  # created in __post_init__
    # Open resources owned by plugins (e.g. device connections), closed in `close()`; any object with .close()
    connections: dict[str, Closable] = field(default_factory=dict)
    output_stream: TextIO | None = None

    def __post_init__(self) -> None:
        """Create the session history, persisted in the workspace if there is one."""
        path = self.workspace.data_dir / "history.jsonl" if self.workspace else None
        self.history = History(path)

    def close(self) -> None:
        """Attempt every resource's cleanup, log failures, and forget the closed session."""
        connections = list(self.connections.items())
        self.connections.clear()
        for name, conn in connections:
            try:
                conn.close()
            except Exception as error:
                self.log(f"warning: cannot close connection '{name}': {error}")

    def log(self, message: str) -> None:
        """Write a progress or diagnostic message to stderr, keeping stdout pipe-clean.

        Args:
            message (str): The message to log.
        """
        print(message, file=sys.stderr)

    def print(self, renderable: RenderableType, *, style: str | None = None) -> None:
        """Print a Rich renderable to stdout in plaintext or rich mode.

        Args:
            renderable: The text or Rich renderable to display.
            style: Optional Rich style applied to the renderable.

        Raises:
            CommandError: If the selected output format is not plaintext or rich.
        """
        stream = self.output_stream if self.output_stream is not None else sys.stdout
        resolved_fmt = resolve_render_format(self.fmt, stream)
        if resolved_fmt not in (RenderFormat.RICH, RenderFormat.PLAINTEXT):
            raise CommandError("Rich output is only available in plaintext or rich format")
        Console(file=stream).print(renderable, style=style, markup=False)

    def run(self, name: str, raw_args: dict[str, Any] | None = None) -> RigboxCmdResult:
        """Validate arguments, invoke the handler, and normalize its response.

        Args:
            name (str): The name of the command to run.
            raw_args (dict[str, Any] | None, optional): The raw arguments to pass to the command. Defaults to None.

        Returns:
            The response containing normalized data and any explicit plaintext message.

        Raises:
            CommandError: If the command is unknown, the args are invalid, or the handler fails.
        """
        cmd = self.registry.get(name)
        try:
            args = cmd.args_model.model_validate(raw_args or {})
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'args'}: {err['msg']}" for err in e.errors())
            raise CommandError(f"{name}: invalid arguments: {problems}") from None
        result = cmd.handler(self, args)
        if cmd.record:
            self.history.append(name, args.model_dump(mode="json", exclude_unset=True))
        return RigboxCmdResult.normalized(result)

    def run_tokens(self, tokens: list[str]) -> RigboxCmdResult:
        """Run a command from tokens: the command name followed by CLI-style arguments.

        Args:
            tokens (list[str]): The command name followed by CLI-style arguments.

        Returns:
            The normalized command response.

        Raises:
            UsageError: If the arguments are invalid or ``--help`` was given; the parser has already printed
                the usage message or help text.
        """
        if not tokens:
            return RigboxCmdResult.empty()
        cmd = self.registry.get(tokens[0])
        return self.run(cmd.name, parse_args(parser_for(cmd), tokens[1:]))

    def run_line(self, line: str) -> RigboxCmdResult:
        """Split `line` into tokens and run it as a command.

        Args:
            line (str): The command line to split and run.

        Returns:
            The normalized command response.
        """
        return self.run_tokens(split_line(line))
