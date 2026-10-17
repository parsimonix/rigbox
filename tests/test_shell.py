import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import NoArgs, Registry
from rigbox.core.workspace import Workspace
from rigbox.repl import RigboxShell, build_shell


@dataclass
class Session:
    """A shell whose standard output is captured in `out`."""

    shell: RigboxShell
    out: io.StringIO

    def run(self, line: str) -> str:
        """Run one line and return what it wrote to stdout (errors go to stderr; use capsys for those)."""
        self.out.seek(0)
        self.out.truncate()
        self.shell.onecmd_plus_hooks(line)
        return self.out.getvalue()

    def tab(self, line: str) -> list[str]:
        """What Tab would offer at the end of `line`."""
        begin = line.rfind(" ") + 1
        return [item.text for item in self.shell.complete(line[begin:], line, begin, len(line)).items]


def make_session(workspace: Workspace | None = None) -> Session:
    reg = Registry()
    load_all(reg, workspace)
    out = io.StringIO()
    return Session(build_shell(Context(registry=reg, workspace=workspace), stdout=out), out)


@pytest.fixture
def session() -> Session:
    return make_session()


def test_every_registry_command_has_its_own_parser(session: Session) -> None:
    """cmd2 keys parsers by function name; a collision would silently give commands someone else's arguments."""
    names = session.shell.ctx.registry.names()
    assert len(names) > 30
    for name in names:
        out = session.run(f"{name} --help")
        assert out.startswith(f"Usage: {name} "), f"{name} shows the wrong help: {out[:80]!r}"


def test_commands_run_and_print_rows(session: Session) -> None:
    out = session.run("demo.echo hi --times 2 --upper")
    assert out.count("HI") == 2 and "message" in out
    assert [entry["run"] for entry in session.shell.ctx.history.entries()] == ["demo.echo"]  # history still recorded


def test_errors_are_reported_without_leaving_the_shell(session: Session, capsys: pytest.CaptureFixture[str]) -> None:
    session.run("dsp.gen foo x.wav")
    assert "invalid choice: 'foo'" in capsys.readouterr().err  # argparse checks Literal arguments
    session.run("dsp.gen sine .wav")
    assert "error: unsupported file type" in capsys.readouterr().err  # a command's own failure
    session.run("demo.echo hi --times 0")
    assert "invalid arguments" in capsys.readouterr().err  # pydantic validation
    session.run("dsp.gem")
    assert "Did you mean dsp.gen?" in capsys.readouterr().err


def test_output_text_is_not_treated_as_markup(session: Session) -> None:
    assert "[bold]x[/bold]" in session.run('demo.echo "[bold]x[/bold]"')


def test_format_is_a_setting(session: Session) -> None:
    session.run("set format json")
    assert json.loads(session.run("demo.echo hi")) == [{"i": 1, "message": "hi"}]
    session.run("set format bogus")
    assert session.shell.format == "json"  # rejected, unchanged


def test_exit_stops_the_shell_like_quit(session: Session) -> None:
    assert session.shell.onecmd_plus_hooks("exit") is True
    assert session.shell.onecmd_plus_hooks("quit") is True


def test_tab_completes_commands_choices_options_and_files(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tone.wav").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert session.tab("dsp.g") == ["dsp.gen"]
    assert {"sine", "square", "step"} <= set(session.tab("dsp.gen s"))
    assert session.tab("dsp.gen sine tone.wav --method ") == ["linear", "logarithmic"]
    assert "--freq" in session.tab("dsp.gen sine tone.wav --f")
    assert session.tab("dsp.inspect t") == ["tone.wav"]  # a path_field argument completes file names
    assert set(session.tab("dsp.inspect ")) >= {"tone.wav", "notes.txt"}


def test_help_groups_commands_by_area(session: Session) -> None:
    out = session.run("help")
    for heading in ("dsp commands", "device commands", "sm commands", "manifest commands"):
        assert heading in out
    assert "dsp.gen" in out and "exit" in out


def test_toolbar_shows_workspace_connections_and_format(tmp_path: Path) -> None:
    session = make_session(Workspace.init(tmp_path / "proj", "bench"))
    shell = session.shell
    assert shell.prompt == "bench> "
    assert shell.get_bottom_toolbar() == " workspace: bench  |  connections: none  |  format: table"
    session.run("device.connect dut loop:// --timeout 0.1")
    session.run("set format json")
    assert shell.get_bottom_toolbar() == " workspace: bench  |  connections: dut  |  format: json"
    shell.ctx.close()
    assert make_session().shell.prompt == "rigbox> "


def test_workspace_plugins_become_shell_commands_and_do_not_leak_between_shells(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj")
    first = make_session(ws)
    first.run("plugin.new greeter")
    second = make_session(ws)  # a fresh registry that sees the new plugin file
    assert hasattr(second.shell, "do_greeter.hello")
    assert not hasattr(make_session().shell, "do_greeter.hello")  # another registry, other commands
    first.run("set format json")
    assert (first.shell.format, second.shell.format) == ("json", "table")  # settings belong to one shell


def test_a_command_named_like_a_builtin_shell_command_is_refused() -> None:
    reg = Registry()
    reg.command("history", args=NoArgs)(lambda ctx, args: None)
    with pytest.raises(ValueError, match="clashes with a built-in shell command"):
        build_shell(Context(registry=reg))
