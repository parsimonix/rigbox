"""Command registry: the single dispatch surface for REPL, CLI and scripts."""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from rigbox.core.transport import Transport, TransportFactory, TransportOptions, scheme_of

if TYPE_CHECKING:
    from rigbox.core.context import Context


class CommandError(Exception):
    """A user-facing error (bad args, unknown command, failed step)."""


class UsageError(CommandError):
    """The command line did not fit the command's arguments, or help was requested.

    The parser has already printed the usage message or the help text, so a caller only needs `exit_code`
    (2 for a bad command line, 0 when help was shown).
    """

    def __init__(self, message: str, exit_code: int) -> None:
        """Remember the parser's `message` and the `exit_code` argparse chose."""
        super().__init__(message)
        self.exit_code = exit_code


class NoArgs(BaseModel):
    """Args model for commands that take no arguments."""


Row = BaseModel | Mapping[str, Any]
"""One output row: a pydantic model or a mapping of column name to value."""

CommandResult = Row | Sequence[Row] | None
"""What a command handler returns: nothing, one row, or several rows."""

Handler = Callable[["Context", Any], CommandResult]
"""A command handler. The second parameter is the command's own args model, so its type varies per command."""


@dataclass(frozen=True)
class Command:
    """A registered command: its name, help text, args model and handler."""

    name: str
    help: str
    args_model: type[BaseModel]
    handler: Handler
    record: bool = True  # whether successful runs go into session history


@dataclass
class PluginInfo:
    """What the loader learned about one plugin: where it came from, its commands, and any load error."""

    name: str
    source: str  # "builtin" | "entry-point" | "workspace"
    commands: list[str] = field(default_factory=list)
    error: str | None = None


class Registry:
    """The set of known commands and transports, plus the plugins that provided them."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._commands: dict[str, Command] = {}
        self.plugins: list[PluginInfo] = []
        self.transports: dict[str, TransportFactory] = {}

    def command(
        self,
        name: str,
        *,
        args: type[BaseModel] = NoArgs,
        help: str = "",
        record: bool = True,
    ) -> Callable[[Handler], Handler]:
        """Return a decorator that registers a handler as command `name`.

        Args:
            name: Dotted command name, e.g. ``manifest.show``.
            args: Pydantic model describing the command's arguments.
            help: One-line help; defaults to the first line of the handler's docstring.
            record: Whether successful runs are recorded in session history.
        """

        def decorator(fn: Handler) -> Handler:
            doc = (fn.__doc__ or "").strip().splitlines()
            self.add(Command(name, help or (doc[0] if doc else ""), args, fn, record))
            return fn

        return decorator

    def add(self, command: Command) -> None:
        """Register `command`.

        Raises:
            ValueError: If a command with the same name is already registered.
        """
        if command.name in self._commands:
            raise ValueError(f"duplicate command: {command.name}")
        self._commands[command.name] = command

    def try_get(self, name: str) -> Command | None:
        """Return the command called `name`, or None if there isn't one."""
        return self._commands.get(name)

    def get(self, name: str) -> Command:
        """Return the command called `name`.

        Raises:
            CommandError: If there is no such command (the message suggests close matches).
        """
        cmd = self._commands.get(name)
        if cmd is None:
            hint = difflib.get_close_matches(name, self._commands, n=3)
            suffix = f" (did you mean: {', '.join(hint)}?)" if hint else ""
            raise CommandError(f"unknown command: {name}{suffix}")
        return cmd

    def names(self) -> list[str]:
        """Return all command names, sorted."""
        return sorted(self._commands)

    def add_transport(self, scheme: str, factory: TransportFactory) -> None:
        """Register `factory` as the way to open URLs of the form ``<scheme>://...``.

        Raises:
            ValueError: If the scheme is already registered.
        """
        scheme = scheme.lower()
        if scheme in self.transports:
            raise ValueError(f"duplicate transport scheme: {scheme}")
        self.transports[scheme] = factory

    def open_transport(self, url: str, options: TransportOptions) -> Transport:
        """Open `url` with the transport registered for its scheme.

        Raises:
            CommandError: If no transport handles the scheme, or opening fails.
        """
        scheme = scheme_of(url)
        factory = self.transports.get(scheme)
        if factory is None:
            known = ", ".join(sorted(self.transports)) or "none"
            raise CommandError(f"no transport for '{scheme}' URLs (known schemes: {known})")
        try:
            return factory(url, options)
        except CommandError:
            raise
        except Exception as e:  # factories are plugin code; never let one crash the host
            raise CommandError(f"cannot open {url}: {e}") from None
