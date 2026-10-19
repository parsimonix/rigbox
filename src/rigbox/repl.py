"""Main REPL entrypoint for Rigbox."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any, ClassVar, TextIO

import cmd2

from rigbox.constants import AppFiles, AppMetadata, RenderFormat
from rigbox.core.cli_parser import parser_for
from rigbox.core.context import Context
from rigbox.core.registry import Command, CommandError


class RigboxShell(cmd2.Cmd):
    """The interactive REPL shell for Rigbox. Commands are registered with `build_shell`, not written here."""

    format: ClassVar[RenderFormat] = RenderFormat.PLAINTEXT
    """How command results are printed; change it with ``set format json``."""
    _INTRO: ClassVar[str] = f"{AppMetadata.NAME} interactive shell. `help` lists commands, `<command> --help` shows its arguments, `exit` quits."

    def __init__(self, ctx: Context, stdout: TextIO | None = None) -> None:
        """Initialize the RigboxShell.

        Args:
            ctx (Context): The Rigbox context containing configuration and workspace information.
            stdout (TextIO | None, optional): The output stream for the shell. Defaults to None.
        """
        history_file = ""
        if ctx.workspace is not None:
            ctx.workspace.data_dir.mkdir(exist_ok=True)
            history_file = str(ctx.workspace.data_dir / AppFiles.SHELL_HISTORY)
        super().__init__(
            allow_cli_args=False,  # NOTE: The shell is started by invoking `rigbox`, whose own arguments are not shell commands
            persistent_history_file=history_file,
            enable_bottom_toolbar=True,
            suggest_similar_command=True,
            intro=self._INTRO,
            stdout=stdout,
        )
        self.ctx = ctx
        self.prompt = f"{ctx.workspace.config.name if ctx.workspace else AppMetadata.NAME}> "
        self.add_settable(cmd2.Settable("format", RenderFormat, "Output format for command results", self, choices=sorted([fmt.value for fmt in RenderFormat])))

    def get_bottom_toolbar(self) -> str:
        """Get the text for the bottom toolbar beneath the shell prompt.

        Returns:
            str: The text to display in the bottom toolbar.
        """
        ws = self.ctx.workspace
        connections = ", ".join(self.ctx.connections) or "none"
        workspace = ws.config.name if ws else "none"
        return f" workspace: {workspace} | connections: {connections} | format: {self.format}"

    def run_command(self, name: str, raw_args: dict[str, Any]) -> None:
        """Run command `name` from this shell's registry, printing its output in the current format or the error.

        Args:
            name (str): The name of the command to run.
            raw_args (dict[str, Any]): The raw arguments to pass to the command.
        """
        self.ctx.fmt = self.format
        self.ctx.output_stream = self.stdout
        try:
            response = self.ctx.run(name, raw_args)
        except CommandError as e:
            self.perror(f"error: {e}")
            return
        text = response.render(self.format, stream=self.stdout)
        if text:
            self.poutput(text)

    @cmd2.with_category(cmd2.Cmd.DEFAULT_CATEGORY)
    def do_exit(self, _: cmd2.Statement) -> bool:
        """Exit the Rigbox shell (same as quit)."""
        return True


def _make_command(command: Command) -> Callable[..., bool | None]:

    # Get a cmd2 parser built for this command
    parser = parser_for(command)

    # Define the function that will be used as the cmd2 command handler
    def do_command(shell: RigboxShell, args: argparse.Namespace) -> None:
        shell.run_command(command.name, {key: value for key, value in vars(args).items() if key in parser.fields})

    # Set required attributes for the cmd2 command method
    do_command.__name__ = f"do_{command.name}"
    do_command.__qualname__ = f"RigboxShell.do_{command.name}"
    do_command.__doc__ = command.help
    # Wrap the command handler with the cmd2 argument parser decorator
    method = cmd2.with_argparser(parser)(do_command)
    # Determine the area/category for the command based on its name
    area = command.name.partition(".")[0] if "." in command.name else "general"
    cmd2.categorize(method, f"{area} commands")
    # Done
    return method


def build_shell(ctx: Context, stdout: TextIO | None = None) -> RigboxShell:
    """Create a RigboxShell whose commands are populated by `ctx.registry`.

    Args:
        ctx (Context): The context containing the registry of commands.
        stdout (TextIO | None, optional): The output stream for the shell. Defaults to None.

    Raises:
        ValueError: If a command in the registry conflicts with an existing built-in RigboxShell method.

    Returns:
        RigboxShell: An instance of RigboxShell with commands populated from the registry.
    """
    methods = {}
    for name in ctx.registry.names():
        if hasattr(RigboxShell, f"do_{name}"):
            raise ValueError(f"Command '{name}' conflicts with an existing built-in RigboxShell method.")
        methods[f"do_{name}"] = _make_command(ctx.registry.get(name))
    return type("RigboxShell", (RigboxShell,), methods)(ctx, stdout)


def run_repl(ctx: Context) -> int:
    """Run the interactive REPL shell until the user quits, and return the exit code.

    Args:
        ctx (Context): The context containing the registry of commands.

    Returns:
        int: The exit code of the REPL shell.
    """
    return int(build_shell(ctx).cmdloop() or 0)
