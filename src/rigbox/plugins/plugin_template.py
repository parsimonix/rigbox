"""Source template for `plugin.new`: a small, fully annotated workspace plugin to edit."""

from __future__ import annotations

import re

PLUGIN_NAME = re.compile(r"[a-z][a-z0-9_]*")

_TEMPLATE = '''"""__NAME__ plugin: an example command to start from. Edit or delete it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rigbox.core.registry import CommandResult, Registry
from rigbox.hooks import hookimpl

if TYPE_CHECKING:
    from rigbox.core.context import Context


class HelloArgs(BaseModel):
    """Arguments for `__NAME__.hello`."""

    who: str = Field("world", description="Who to greet")


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `__NAME__.*` commands."""

    @registry.command("__NAME__.hello", args=HelloArgs, record=False)
    def hello(ctx: Context, args: HelloArgs) -> CommandResult:
        """Greet someone (example command; replace it)."""
        return {"greeting": f"hello, {args.who}"}
'''


def render_plugin(name: str) -> str:
    """Return the template source for a plugin called `name` (which must match `PLUGIN_NAME`)."""
    return _TEMPLATE.replace("__NAME__", name)
