"""Command registry: the single dispatch surface for REPL, CLI and scripts."""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias, TypeVar

from pydantic import BaseModel
from rich.console import RenderableType

from rigbox.core.render import RigboxCmdResult

if TYPE_CHECKING:
    from rigbox.core.context import Context

ExtensionT = TypeVar("ExtensionT")


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


StructuredData: TypeAlias = BaseModel | Mapping[str, Any]
"""Structured data: a pydantic model or a mapping of keys to values."""

CommandResult: TypeAlias = RigboxCmdResult | str | RenderableType | StructuredData | Sequence[StructuredData] | None
"""A handler's explicit response, plaintext message, or legacy structured data."""

Handler: TypeAlias = Callable[["Context", Any], CommandResult]
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
    """The known commands and plugin-owned extensions, plus their providing plugins."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._commands: dict[str, Command] = {}
        self.plugins: list[PluginInfo] = []
        self._extensions: dict[type[object], object] = {}

    def extension(self, extension_type: type[ExtensionT]) -> ExtensionT:
        """Get or create one plugin-owned extension per registry, independent of plugin load order.

        Args:
            extension_type: Extension class with a no-argument constructor.

        Returns:
            The shared instance for this registry and extension class.
        """
        extension = self._extensions.get(extension_type)
        if isinstance(extension, extension_type):
            return extension
        extension = extension_type()
        self._extensions[extension_type] = extension
        return extension

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

        Args:
            command (Command): The command to register.

        Raises:
            ValueError: If a command with the same name is already registered.
        """
        if command.name in self._commands:
            raise ValueError(f"duplicate command: {command.name}")
        self._commands[command.name] = command

    def try_get(self, name: str) -> Command | None:
        """Return the command called `name`, or None if there isn't one.

        Args:
            name (str): The name of the command to retrieve.

        Returns:
            Command | None: The command if found, otherwise None.
        """
        return self._commands.get(name)

    def get(self, name: str) -> Command:
        """Return the command called `name`.

        Args:
            name (str): The name of the command to retrieve.

        Returns:
            Command: The command if found.

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
        """Return all command names, sorted.

        Returns:
            list[str]: The sorted list of all command names.
        """
        return sorted(self._commands)
