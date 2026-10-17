import subprocess
import sys
from pathlib import Path

import pytest

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.workspace import Workspace
from rigbox.plugins.plugin_template import render_plugin

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def context_for(ws: Workspace) -> Context:
    reg = Registry()
    load_all(reg, ws)
    return Context(registry=reg, workspace=ws)


def test_plugin_new_writes_a_plugin_that_loads_and_runs(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj")
    (row,) = context_for(ws).run("plugin.new", {"name": "greeter"})
    assert row["command"] == "greeter.hello" and Path(row["written"]) == ws.plugins_dir / "greeter.py"

    fresh = context_for(ws)  # a new session picks the plugin up
    assert fresh.run("greeter.hello", {"who": "bench"}) == [{"greeting": "hello, bench"}]
    assert fresh.run("greeter.hello") == [{"greeting": "hello, world"}]
    greeter = next(p for p in fresh.registry.plugins if p.name == "greeter")
    assert greeter.source == "workspace" and greeter.error is None and greeter.commands == ["greeter.hello"]


def test_generated_plugin_meets_the_repo_lint_standard(tmp_path: Path) -> None:
    plugin = tmp_path / "greeter.py"
    plugin.write_text(render_plugin("greeter"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--config", str(PYPROJECT), str(plugin)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_plugin_new_refuses_bad_input(tmp_path: Path) -> None:
    ctx = context_for(Workspace.init(tmp_path / "proj"))
    for bad in ("Bad", "1abc", "has-dash", "has space", ""):
        with pytest.raises(CommandError, match="invalid plugin name"):
            ctx.run("plugin.new", {"name": bad})
    bare = Context(registry=ctx.registry)  # no workspace
    with pytest.raises(CommandError, match="needs a workspace"):
        bare.run("plugin.new", {"name": "ok"})


def test_plugin_new_does_not_overwrite_without_force(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj")
    ctx = context_for(ws)
    ctx.run("plugin.new", {"name": "mine"})
    target = ws.plugins_dir / "mine.py"
    target.write_text("# my edits\n", encoding="utf-8")
    with pytest.raises(CommandError, match="already exists"):
        ctx.run("plugin.new", {"name": "mine"})
    assert target.read_text(encoding="utf-8") == "# my edits\n"
    ctx.run("plugin.new", {"name": "mine", "force": True})
    assert "def rigbox_register" in target.read_text(encoding="utf-8")
