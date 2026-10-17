"""Plugin discovery on top of pluggy: built-ins, installed entry points, workspace-local files.

pluggy owns hook definition and dispatch; this module only gathers plugin modules from our three
sources and records per-plugin failures (a broken plugin never aborts startup).
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable
from functools import partial
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

import pluggy

from rigbox import hooks
from rigbox.constants import Plugins as PluginConstants
from rigbox.core.registry import PluginInfo, Registry
from rigbox.core.workspace import Workspace


def load_all(registry: Registry, workspace: Workspace | None = None) -> None:
    """Load every plugin and let each register its commands on `registry`.

    Failures are recorded in ``registry.plugins`` rather than raised.
    """
    pm = pluggy.PluginManager(PluginConstants.PROJECT)
    pm.add_hookspecs(hooks)
    sources: dict[str, str] = {}

    def add(name: str, source: str, loader: Callable[[], object]) -> None:
        try:
            pm.register(loader(), name=name)
            sources[name] = source
        except Exception as e:  # plugin code must never crash the host
            registry.plugins.append(PluginInfo(name, source, error=f"{type(e).__name__}: {e}"))

    for mod in PluginConstants.BUILTIN_MODULES:
        add(mod, "builtin", partial(importlib.import_module, mod))
    for ep in entry_points(group=PluginConstants.ENTRYPOINT_GROUP):
        add(ep.name, "entry-point", ep.load)
    if workspace is not None and workspace.plugins_dir.is_dir():
        for path in sorted(workspace.plugins_dir.glob("*.py")):
            if not path.name.startswith("_"):
                add(path.stem, "workspace", partial(_import_file, path))

    called: set[str] = set()
    for impl in pm.hook.rigbox_register.get_hookimpls():
        called.add(impl.plugin_name)
        before = set(registry.names())
        info = PluginInfo(impl.plugin_name, sources[impl.plugin_name])
        try:
            impl.function(registry=registry)
        except Exception as e:
            info.error = f"{type(e).__name__}: {e}"
        info.commands = sorted(set(registry.names()) - before)
        registry.plugins.append(info)
    for name, source in sources.items():
        if name not in called:
            registry.plugins.append(PluginInfo(name, source, error="no rigbox_register hook"))


def _import_file(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"rigbox_workspace_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
