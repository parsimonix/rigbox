# Built-in utilities and adding plugins

Domain-specific commands are documented in the [manifests](manifests/README.md), [device](device/README.md),
[state machine](statemachine/README.md), [code generation](codegen/README.md), and [DSP](dsp/README.md) READMEs.

## Built-in utilities

The [`builtin.py`](builtin.py) plugin provides command listing, workspace information, workflows, plugin listing
and scaffolding, and a demo command.

```powershell
rigbox run commands --prefix dsp.    # narrow the command list
rigbox run plugins.list              # loaded plugins and loading errors
rigbox run workspace.info
rigbox run --format json demo.echo hi --times 2
```

Commands you run are recorded as structured steps; `workflow.save <name> --last N` turns the last N into a shareable
YAML workflow in `workflows/`, and `workflow.run <name>` replays it.

## Adding commands

A plugin is a module with one hook function. The quickest start is to scaffold one inside a workspace:

```powershell
rigbox run plugin.new greeter     # writes plugins/greeter.py with an example command (restart rigbox to load it)
rigbox run greeter.hello --who bench
```

Or write it by hand and drop it in `<workspace>/plugins/` (or ship it as an entry point in the `rigbox.plugins`
group):

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rigbox.core.registry import CommandResult, Registry
from rigbox.hooks import hookimpl

if TYPE_CHECKING:
    from rigbox.core.context import Context


class HelloArgs(BaseModel):
    """Arguments for `greeter.hello`."""

    who: str = Field("world", description="Who to greet")


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `greeter.*` commands."""

    @registry.command("greeter.hello", args=HelloArgs, record=False)
    def hello(ctx: Context, args: HelloArgs) -> CommandResult:
        """Greet someone."""
        return {"greeting": f"hello, {args.who}"}
```

Commands take a pydantic args model and may return a `RigboxResponse` containing structured data and an optional
`HumanReadableResponse(text, type)` message (both from `rigbox.core.render`). Plain dict/model/list results remain
supported and fall back to tables in human mode. `Context.run(...)` returns a response, not a list; access
`.structured_data` for normalized rows. Raise `CommandError` for user mistakes.
Contributors adding to the repo itself can use the project skills in [`.claude/skills/`](../../../.claude/skills/)
(`onboard-me`, `add-plugin`, `add-command`).
