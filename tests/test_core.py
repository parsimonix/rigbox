import json
from pathlib import Path

import pytest

from rigbox.cli import main
from rigbox.core.cli_parser import split_line
from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.render import render
from rigbox.core.workflow import substitute
from rigbox.core.workspace import Workspace


@pytest.fixture
def ctx() -> Context:
    reg = Registry()
    load_all(reg)
    return Context(registry=reg)


def test_split_line_keeps_windows_paths_and_quotes() -> None:
    assert split_line(r'load "C:\a b\x.yaml" --n 2') == ["load", r"C:\a b\x.yaml", "--n", "2"]


def test_unknown_command_suggests(ctx: Context) -> None:
    with pytest.raises(CommandError, match="did you mean"):
        ctx.registry.get("demo.ech")


def test_commands_lists_every_command_or_those_with_a_prefix(ctx: Context) -> None:
    listed = {row["command"] for row in ctx.run_line("commands")}
    assert {"commands", "dsp.gen", "device.connect", "sm.fuzz"} <= listed
    assert "help" not in listed and "history" not in listed  # the shell provides these itself
    only_dsp = ctx.run_line("commands --prefix dsp.")
    assert only_dsp and all(row["command"].startswith("dsp.") for row in only_dsp)
    assert all(row["summary"] for row in only_dsp)


def test_run_line_validates_and_returns_rows(ctx: Context) -> None:
    rows = ctx.run_line("demo.echo hi --times 2 --upper")
    assert rows == [{"i": 1, "message": "HI"}, {"i": 2, "message": "HI"}]
    with pytest.raises(CommandError, match="invalid arguments"):
        ctx.run_line("demo.echo hi --times 0")


def test_history_records_only_recordable_commands(ctx: Context) -> None:
    ctx.run_line("demo.echo a")
    ctx.run_line("commands")
    assert [e["run"] for e in ctx.history.entries()] == ["demo.echo"]


def test_render_formats() -> None:
    rows = [{"a": 1, "b": "x"}, {"a": 22, "b": "y"}]
    assert json.loads(render(rows, "json")) == rows
    assert [json.loads(line) for line in render(rows, "ndjson").splitlines()] == rows
    assert render(rows, "table").splitlines()[0].split() == ["a", "b"]
    assert render([], "json") == "[]"


def test_substitute_preserves_type_for_whole_values() -> None:
    assert substitute({"n": "${x}", "s": "v=${x}"}, {"x": 5}) == {"n": 5, "s": "v=5"}
    with pytest.raises(CommandError):
        substitute("${missing}", {})


def test_workspace_init_and_find(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj", "p")
    assert (ws.root / "rigbox.yaml").is_file() and ws.workflows_dir.is_dir()
    found = Workspace.find(ws.root / "plugins")
    assert found is not None and found.root == ws.root
    with pytest.raises(FileExistsError):
        Workspace.init(ws.root)


def test_workspace_plugin_loading_and_failure_isolation(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj")
    (ws.plugins_dir / "hello.py").write_text(
        "from rigbox.hooks import hookimpl\n"
        "@hookimpl\n"
        "def rigbox_register(registry):\n"
        "    @registry.command('hello.say')\n"
        "    def say(ctx, args):\n"
        "        return {'msg': 'hi'}\n",
        encoding="utf-8",
    )
    (ws.plugins_dir / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    (ws.plugins_dir / "nohook.py").write_text("X = 1\n", encoding="utf-8")
    reg = Registry()
    load_all(reg, ws)
    ctx = Context(registry=reg, workspace=ws)
    assert ctx.run("hello.say") == [{"msg": "hi"}]
    by_name = {p.name: p for p in reg.plugins}
    assert by_name["hello"].commands == ["hello.say"]
    broken, nohook = by_name["broken"].error, by_name["nohook"].error
    assert broken is not None and "boom" in broken
    assert nohook is not None and "no rigbox_register hook" in nohook


def test_history_to_workflow_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = Workspace.init(tmp_path / "proj")
    reg = Registry()
    load_all(reg, ws)
    ctx = Context(registry=reg, workspace=ws)
    ctx.run_line("demo.echo one")
    ctx.run_line("demo.echo two --times 2")
    ctx.run_line("workflow.save twice --last 2")
    assert (ws.workflows_dir / "twice.yaml").is_file()
    rows = ctx.run_line("workflow.run twice")
    assert [r["message"] for r in rows] == ["two", "two"]
    # history persisted to disk for the next session
    assert (ws.data_dir / "history.jsonl").is_file()


def test_cli_run_and_script_modes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = Workspace.init(tmp_path / "proj")
    wf = ws.workflows_dir / "greet.yaml"
    wf.write_text(
        "name: greet\nvars: {who: world}\nsteps:\n  - run: demo.echo\n    args: {message: 'hi ${who}'}\n",
        encoding="utf-8",
    )
    assert main(["--workspace", str(ws.root), "run", "--format", "json", "demo.echo", "x"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"i": 1, "message": "x"}]

    assert main(["--workspace", str(ws.root), "--format", "ndjson", "script", str(wf), "--var", "who=you"]) == 0
    assert json.loads(capsys.readouterr().out) == {"i": 1, "message": "hi you"}

    assert main(["--workspace", str(ws.root), "run", "nope"]) == 1


def test_cli_global_options_work_before_or_after_the_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = Workspace.init(tmp_path / "proj")  # the current directory is not inside this workspace
    assert main(["--workspace", str(ws.root), "run", "plugin.new", "before"]) == 0
    assert main(["run", "--workspace", str(ws.root), "plugin.new", "after"]) == 0
    assert (ws.plugins_dir / "before.py").is_file() and (ws.plugins_dir / "after.py").is_file()

    capsys.readouterr()
    assert main(["--format", "json", "run", "demo.echo", "hi"]) == 0  # json, not the "auto" default, so a list
    assert json.loads(capsys.readouterr().out) == [{"i": 1, "message": "hi"}]
