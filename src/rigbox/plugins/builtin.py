"""Built-in commands: command listing, workspace, workflows, plugin listing and scaffolding, demo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rigbox.core.cli_parser import path_field
from rigbox.core.context import Context
from rigbox.core.registry import CommandError, NoArgs, Registry
from rigbox.core.render import RigboxResponse
from rigbox.core.workflow import Step, Workflow, load_workflow, run_workflow, save_workflow
from rigbox.hooks import hookimpl
from rigbox.plugins.plugin_template import PLUGIN_NAME, render_plugin


class CommandsArgs(BaseModel):
    """Arguments for `commands`."""

    prefix: str = Field("", description="Only list commands starting with this, e.g. 'dsp.'")


class WorkflowSaveArgs(BaseModel):
    """Arguments for `workflow.save`."""

    name: str
    last: int = Field(1, description="Save the last N successful history steps")
    description: str = ""


class WorkflowRunArgs(BaseModel):
    """Arguments for `workflow.run`."""

    workflow: str = path_field(description="Path to a workflow file, or a name in the workspace workflows dir")
    var: list[str] = Field(default_factory=list, description="Override a variable: --var key=value")


class EchoArgs(BaseModel):
    """Arguments for `demo.echo`."""

    message: str
    times: int = Field(1, ge=1, description="Repeat count")
    upper: bool = False


class PluginNewArgs(BaseModel):
    """Arguments for `plugin.new`."""

    name: str = Field(description="Plugin name: lowercase letters, digits and underscores, starting with a letter")
    force: bool = Field(False, description="Overwrite the plugin file if it already exists")


def _find_workflow(ctx: Context, ref: str) -> Path:
    p = Path(ref)
    if p.is_file():
        return p
    if ctx.workspace is not None:
        found = ctx.workspace.find_file(ctx.workspace.workflows_dir, ref)
        if found:
            return found
    raise CommandError(f"workflow not found: {ref}")


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the built-in commands."""

    @registry.command("commands", args=CommandsArgs, record=False)
    def commands(ctx: Context, args: CommandsArgs) -> list[dict[str, Any]]:
        """List the commands (use `<command> --help` for one command's arguments)."""
        return [{"command": name, "summary": ctx.registry.get(name).help} for name in ctx.registry.names() if name.startswith(args.prefix)]

    @registry.command("plugins.list", record=False)
    def plugins_list(ctx: Context, args: NoArgs) -> list[dict[str, Any]]:
        """List loaded plugins and their commands."""
        return [{"plugin": p.name, "source": p.source, "commands": len(p.commands), "error": p.error} for p in ctx.registry.plugins]

    @registry.command("plugin.new", args=PluginNewArgs, record=False)
    def plugin_new(ctx: Context, args: PluginNewArgs) -> dict[str, str]:
        """Create a starter plugin file in the workspace plugins folder."""
        if ctx.workspace is None:
            raise CommandError("plugin.new needs a workspace (run `rigbox init`)")
        if not PLUGIN_NAME.fullmatch(args.name):
            raise CommandError(f"invalid plugin name '{args.name}': use lowercase letters, digits and underscores")
        path = ctx.workspace.plugins_dir / f"{args.name}.py"
        if path.exists() and not args.force:
            raise CommandError(f"{path.name} already exists (use --force to overwrite)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_plugin(args.name), encoding="utf-8")
        ctx.log("restart rigbox to load the new plugin")
        return {"written": str(path), "command": f"{args.name}.hello"}

    @registry.command("workspace.info", record=False)
    def workspace_info(ctx: Context, args: NoArgs) -> list[dict[str, Any]]:
        """Show the active workspace."""
        if ctx.workspace is None:
            return [{"workspace": None}]
        ws = ctx.workspace
        return [{"name": ws.config.name, "root": str(ws.root)}]

    @registry.command("workflow.save", args=WorkflowSaveArgs, record=False)
    def workflow_save(ctx: Context, args: WorkflowSaveArgs) -> dict[str, Any]:
        """Save the last N history steps as a shareable workflow file."""
        if ctx.workspace is None:
            raise CommandError("workflow.save needs a workspace (run `rigbox init`)")
        steps = [Step(run=e["run"], args=e["args"]) for e in ctx.history.entries() if e["ok"]][-args.last :]
        if not steps:
            raise CommandError("no history to save")
        path = ctx.workspace.workflows_dir / f"{args.name}.yaml"
        save_workflow(path, Workflow(name=args.name, description=args.description, steps=steps))
        return {"saved": str(path), "steps": len(steps)}

    @registry.command("workflow.run", args=WorkflowRunArgs, record=False)
    def workflow_run(ctx: Context, args: WorkflowRunArgs) -> RigboxResponse:
        """Run a declarative workflow file."""
        overrides: dict[str, Any] = {}
        for item in args.var:
            key, eq, value = item.partition("=")
            if not eq:
                raise CommandError(f"--var expects key=value, got: {item}")
            overrides[key] = value
        return run_workflow(ctx, load_workflow(_find_workflow(ctx, args.workflow)), overrides)

    @registry.command("demo.echo", args=EchoArgs)
    def demo_echo(ctx: Context, args: EchoArgs) -> list[dict[str, Any]]:
        """Echo a message (smoke-test command)."""
        text = args.message.upper() if args.upper else args.message
        return [{"i": i + 1, "message": text} for i in range(args.times)]
