"""The plugin contract. Plugin authors import `hookimpl` from here.

Example:
    from rigbox.hooks import hookimpl

    @hookimpl
    def rigbox_register(registry):
        @registry.command("hello.say")
        def say(ctx, args):
            return {"msg": "hi"}
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pluggy

from rigbox.constants import Plugins

if TYPE_CHECKING:
    from rigbox.core.registry import Registry

hookspec = pluggy.HookspecMarker(Plugins.PROJECT)
hookimpl = pluggy.HookimplMarker(Plugins.PROJECT)


@hookspec
def rigbox_register(registry: Registry) -> None:
    """Add this plugin's commands to `registry`."""
