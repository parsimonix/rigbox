"""Main CLI entrypoint for Rigbox."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from rigbox.constants import RenderFormat, RenderLevel
from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry, UsageError
from rigbox.core.render import RenderError, RigboxCmdResult
from rigbox.core.workspace import Workspace

cli = typer.Typer()


def _build_context(workspace: str | None, fmt: RenderFormat) -> Context:
    """Find the workspace (from `workspace` or the cwd), load plugins and return a ready Context."""
    ws = Workspace.find(Path(workspace) if workspace else Path.cwd())
    registry = Registry()
    load_all(registry, ws)
    return Context(registry=registry, workspace=ws, fmt=fmt)


@cli.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Main entrypoint for the Rigbox CLI. Overridden to allow for REPL launch when no arguments are provided.

    Args:
        ctx (typer.Context): The Typer context object for the CLI invocation.
    """
    # If no subcommand was invoked, we default to spawning the interactive REPL interface
    if ctx.invoked_subcommand is None:
        from rigbox.repl import run_repl

        rigbox_ctx = _build_context(None, RenderFormat.default())
        run_repl(rigbox_ctx)


@cli.command(help="Initialize a new Rigbox workspace.")
def init(
    path: Path = typer.Argument(Path("."), help="The path to create the workspace in."),
    name: str | None = typer.Option(None, "--name", help="The name of the workspace."),
) -> None:
    """Initialize a new Rigbox workspace.

    Args:
        path (Path, optional): The path to create the workspace in. Defaults to the current directory.
        name (str | None, optional): The name of the workspace. Defaults to None.

    Raises:
        typer.Exit(code=1): Raised if the workspace already exists.
        typer.Exit(code=0): Raised after successful initialization.
    """
    try:
        ws = Workspace.init(path, name)
    except FileExistsError as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1) from e
    typer.echo(f"Workspace initialized at {ws.root}")
    raise typer.Exit(code=0)


@cli.command(context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False}, help="Run one command non-interactively.")
def run(
    tokens: Annotated[list[str], typer.Argument(help="<command> [args...]")],
    workspace: Annotated[str | None, typer.Option(help="Start the workspace search here (default: cwd)")] = None,
    fmt: Annotated[RenderFormat, typer.Option("--format", "-f", help=f"Output format (default: {RenderFormat.default()})")] = RenderFormat.default(),
) -> None:
    """Run one command non-interactively.

    Args:
        tokens (Annotated[list[str], typer.Argument): The command and its arguments to run.
        workspace (str | None, optional): The workspace to start the search from. Defaults to cwd.
        fmt: The output format, including plain text, tables, JSON, and NDJSON. Defaults to "auto".

    Raises:
        typer.Exit(code=2): Raised if no command is specified.
        typer.Exit(code=2): Raised if there is a usage error.
        typer.Exit(code=1): Raised if there is a command error.
        typer.Exit(code=0): Raised after successful execution.
    """
    if not tokens:
        typer.echo("Error: No command specified.")
        raise typer.Exit(code=2)
    ctx = _build_context(workspace, fmt)
    try:
        response = ctx.run_tokens(tokens)
        out = response.render(fmt)
    except UsageError as e:
        raise typer.Exit(code=e.exit_code) from e
    except (CommandError, RenderError) as e:
        typer.echo(RigboxCmdResult(text=str(e), level=RenderLevel.ERROR).render(RenderFormat.PLAINTEXT), err=True)
        raise typer.Exit(code=1) from e
    finally:
        ctx.close()
    if out:
        typer.echo(out)
    raise typer.Exit(code=0)


@cli.command(help="Execute a script file within the Rigbox workspace.")
def script(
    file: Annotated[Path, typer.Argument(help="The script file to execute.")],
    workspace: Annotated[str | None, typer.Option(help="Start the workspace search here (default: cwd)")] = None,
    fmt: Annotated[RenderFormat, typer.Option("--format", "-f", help=f"Output format (default: {RenderFormat.default()})")] = RenderFormat.default(),
    var: Annotated[list[str] | None, typer.Option("--var", "-v", help="key=value override")] = None,
) -> None:
    """Execute a script file within the Rigbox workspace.

    Args:
        file (Annotated[Path, typer.Argument): The script file to execute.
        workspace (str | None, optional): The workspace to start the search from. Defaults to cwd.
        fmt: The output format, including plain text, rich renderables, JSON, and NDJSON. Defaults to "auto".
        var (Annotated[list[str] | None, typer.Option, optional): Key=value overrides for the script. Defaults to None.

    Raises:
        typer.Exit(code=2): Raised if there is a usage error.
        typer.Exit(code=1): Raised if there is a command error.
        typer.Exit(code=0): Raised after successful execution.
    """
    ctx = _build_context(workspace, fmt)
    try:
        response = ctx.run("workflow.run", {"workflow": str(file), "var": var or []})
        out = response.render(fmt)
    except UsageError as e:
        raise typer.Exit(code=e.exit_code) from e
    except (CommandError, RenderError) as e:
        typer.echo(RigboxCmdResult(text=str(e), level=RenderLevel.ERROR).render(RenderFormat.PLAINTEXT), err=True)
        raise typer.Exit(code=1) from e
    finally:
        ctx.close()
    if out:
        typer.echo(out)
    raise typer.Exit(code=0)


if __name__ == "__main__":
    cli()
