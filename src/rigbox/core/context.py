"""Execution context shared by REPL, CLI and workflows."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from rigbox.core.cli_parser import parse_args, parser_for, split_line
from rigbox.core.history import History
from rigbox.core.registry import CommandError, Registry
from rigbox.core.render import normalize
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
    fmt: str = "auto"
    history: History = field(init=False)  # created in __post_init__
    # Open resources owned by plugins (e.g. device connections), closed in `close()`; any object with .close()
    connections: dict[str, Closable] = field(default_factory=dict)

    def close(self) -> None:
        """Close every open connection and forget them."""
        for conn in self.connections.values():
            conn.close()
        self.connections.clear()

    def __post_init__(self) -> None:
        """Create the session history, persisted in the workspace if there is one."""
        path = self.workspace.data_dir / "history.jsonl" if self.workspace else None
        self.history = History(path)

    def log(self, message: str) -> None:
        """Write a progress or diagnostic message to stderr, keeping stdout pipe-clean."""
        print(message, file=sys.stderr)

    def run(self, name: str, raw_args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Validate args, invoke the handler, record history, return normalised rows.

        Raises:
            CommandError: If the command is unknown, the args are invalid, or the handler fails.
        """
        cmd = self.registry.get(name)
        try:
            args = cmd.args_model.model_validate(raw_args or {})
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'args'}: {err['msg']}" for err in e.errors()
            )
            raise CommandError(f"{name}: invalid arguments: {problems}") from None
        result = cmd.handler(self, args)
        if cmd.record:
            self.history.append(name, args.model_dump(mode="json", exclude_unset=True))
        return normalize(result)

    def run_tokens(self, tokens: list[str]) -> list[dict[str, Any]]:
        """Run a command from tokens: the command name followed by CLI-style arguments.

        Raises:
            UsageError: If the arguments are invalid or ``--help`` was given; the parser has already printed
                the usage message or help text.
        """
        if not tokens:
            return []
        cmd = self.registry.get(tokens[0])
        return self.run(cmd.name, parse_args(parser_for(cmd), tokens[1:]))

    def run_line(self, line: str) -> list[dict[str, Any]]:
        """Split `line` into tokens and run it as a command."""
        return self.run_tokens(split_line(line))
