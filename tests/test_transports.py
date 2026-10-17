import json
from pathlib import Path

import pytest

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.transport import DEFAULT_SCHEME, Transport, TransportOptions, scheme_of
from rigbox.core.workspace import Workspace

EXAMPLE = str(Path(__file__).parent.parent / "examples" / "sensor_report.yaml")
VALUES = "{id: %d, temperature: 2.5, ok: true, label: node1, samples: [1,-2,3,4], blob: dead}"

# A third-party transport, supplied by a workspace plugin: it answers every write with the same bytes reversed.
STUB_PLUGIN = '''
from rigbox.core.registry import Registry
from rigbox.core.transport import Transport, TransportOptions
from rigbox.hooks import hookimpl

SEEN: list[TransportOptions] = []


class ReverseTransport:
    def __init__(self) -> None:
        self._pending = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self._pending += data[::-1]

    def read(self, size: int, timeout: float) -> bytes:
        out, self._pending = self._pending[:size], self._pending[size:]
        return out

    def read_chunk(self, timeout: float) -> bytes:
        out, self._pending = self._pending, b""
        return out

    def close(self) -> None:
        self.closed = True


def open_reverse(url: str, options: TransportOptions) -> Transport:
    """Echo writes back reversed: reverse://."""
    SEEN.append(options)
    return ReverseTransport()


@hookimpl
def rigbox_register(registry: Registry) -> None:
    registry.add_transport("reverse", open_reverse)
'''


@pytest.fixture
def ctx() -> Context:
    reg = Registry()
    load_all(reg)
    return Context(registry=reg)


def make_capture(ctx: Context, tmp_path: Path, ids: list[int]) -> Path:
    """Record manifest packets sent over loopback into a capture file."""
    ctx.run("device.connect", {"name": "src", "url": "loop://", "timeout": 0.1})
    for i in ids:
        ctx.run("device.send", {"name": "src", "manifest": EXAMPLE, "values": VALUES % i})
    path = tmp_path / "cap.ndjson"
    ctx.run("device.capture", {"name": "src", "manifest": EXAMPLE, "count": len(ids), "seconds": 5, "out": str(path)})
    ctx.run("device.close", {"name": "src"})
    return path


def test_scheme_of() -> None:
    assert scheme_of("loop://") == "loop" and scheme_of("REPLAY://x.ndjson") == "replay"
    assert scheme_of("COM3") == DEFAULT_SCHEME == scheme_of("/dev/ttyUSB0") == scheme_of("serial://COM3")


def test_builtin_transports_are_listed_with_descriptions(ctx: Context) -> None:
    rows = {r["scheme"]: r["description"] for r in ctx.run("device.transports")}
    assert set(rows) == {"serial", "loop", "socket", "rfc2217", "replay"}
    assert "loop://" in rows["loop"] and "replay://" in rows["replay"]


def test_registry_rejects_duplicate_and_unknown_schemes(ctx: Context) -> None:
    with pytest.raises(ValueError, match="duplicate transport scheme: loop"):
        ctx.registry.add_transport("LOOP", lambda url, options: ctx.registry.open_transport("loop://", options))
    with pytest.raises(CommandError, match=r"known schemes: loop, replay, rfc2217, serial, socket"):
        ctx.registry.open_transport("nope://", TransportOptions())


def test_factory_failures_never_escape_as_raw_exceptions() -> None:
    reg = Registry()

    def broken(url: str, options: TransportOptions) -> Transport:
        raise RuntimeError("boom")

    reg.add_transport("broken", broken)
    with pytest.raises(CommandError, match=r"cannot open broken://x: boom"):
        reg.open_transport("broken://x", TransportOptions())


def test_replay_decodes_recorded_packets_in_order_then_reports_the_end(ctx: Context, tmp_path: Path) -> None:
    cap = make_capture(ctx, tmp_path, [1, 2, 3])
    ctx.run("device.connect", {"name": "r", "url": f"replay://{cap}"})
    assert [ctx.run("device.expect", {"name": "r", "manifest": EXAMPLE})[0]["id"] for _ in range(3)] == [1, 2, 3]
    with pytest.raises(CommandError, match=r"stream ended: got 0 of 25 bytes"):
        ctx.run("device.expect", {"name": "r", "manifest": EXAMPLE})
    (row,) = ctx.run("device.list")
    assert (row["name"], row["transport"]) == ("r", "replay")


def test_replay_keeps_capture_boundaries_and_splits_reads(ctx: Context, tmp_path: Path) -> None:
    cap = tmp_path / "raw.ndjson"
    cap.write_text(
        "\n".join(json.dumps(r) for r in [{"dir": "rx", "hex": "0102"}, {"dir": "tx", "hex": "ff"}, {"hex": "030405"}])
        + "\n",
        encoding="utf-8",
    )
    ctx.run("device.connect", {"name": "r", "url": f"replay://{cap}"})
    assert ctx.run("device.expect", {"name": "r", "size": 1}) == [{"size": 1, "hex": "01"}]  # splits a packet
    assert ctx.run("device.expect", {"name": "r", "size": 3}) == [{"size": 3, "hex": "020304"}]  # spans packets
    assert ctx.run("device.expect", {"name": "r", "size": 1}) == [{"size": 1, "hex": "05"}]
    with pytest.raises(CommandError, match="stream ended"):
        ctx.run("device.expect", {"name": "r", "size": 1})


def test_replay_can_be_recaptured_and_stops_at_the_end(ctx: Context, tmp_path: Path) -> None:
    cap = make_capture(ctx, tmp_path, [7, 8])
    ctx.run("device.connect", {"name": "r", "url": f"replay://{cap}"})
    out = tmp_path / "again.ndjson"
    (row,) = ctx.run("device.capture", {"name": "r", "manifest": EXAMPLE, "seconds": 30, "out": str(out)})
    assert (row["packets"], row["errors"]) == (2, 0)  # returned immediately, not after 30 s
    ids = [json.loads(line)["decoded"]["id"] for line in out.read_text(encoding="utf-8").splitlines()]
    assert ids == [7, 8]


def test_replay_ignores_writes_and_reports_bad_files(ctx: Context, tmp_path: Path) -> None:
    cap = make_capture(ctx, tmp_path, [1])
    ctx.run("device.connect", {"name": "r", "url": f"replay://{cap}"})
    assert ctx.run("device.send", {"name": "r", "hex": "00"})[0]["sent"] == 1  # accepted, then discarded
    with pytest.raises(CommandError, match="capture file not found"):
        ctx.run("device.connect", {"name": "x", "url": f"replay://{tmp_path / 'missing.ndjson'}"})
    bad = tmp_path / "bad.ndjson"
    bad.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(CommandError, match="cannot read capture"):
        ctx.run("device.connect", {"name": "x", "url": f"replay://{bad}"})


def test_third_party_plugin_can_add_a_transport(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj")
    (ws.plugins_dir / "reverser.py").write_text(STUB_PLUGIN, encoding="utf-8")
    reg = Registry()
    load_all(reg, ws)
    ctx = Context(registry=reg, workspace=ws)
    assert "reverse" in {r["scheme"] for r in ctx.run("device.transports")}

    ctx.run("device.connect", {"name": "rev", "url": "reverse://", "timeout": 0.25, "param": ["mode=fast", "x=1=2"]})
    ctx.run("device.send", {"name": "rev", "hex": "01 02 03"})
    assert ctx.run("device.expect", {"name": "rev", "size": 3}) == [{"size": 3, "hex": "030201"}]
    ctx.close()  # closes through the connection wrapper without knowing what the transport is


def test_connect_passes_options_and_rejects_bad_params() -> None:
    reg = Registry()
    load_all(reg)
    seen: list[TransportOptions] = []

    def recording(url: str, options: TransportOptions) -> Transport:
        seen.append(options)
        return reg.open_transport("loop://", options)

    reg.add_transport("rec", recording)
    ctx = Context(registry=reg)
    ctx.run("device.connect", {"name": "a", "url": "rec://", "baud": 9600, "timeout": 0.5, "param": ["k=v", "e=a=b"]})
    assert seen == [TransportOptions(timeout=0.5, baud=9600, params={"k": "v", "e": "a=b"})]
    for bad in ("novalue", "=x"):
        with pytest.raises(CommandError, match="--param expects key=value"):
            ctx.run("device.connect", {"name": "b", "url": "rec://", "param": [bad]})
    ctx.close()


def test_repeated_param_options_parse_from_a_command_line(ctx: Context) -> None:
    ctx.run_line("device.connect d loop:// --param a=1 --param b=2 --timeout 0.1")
    assert ctx.run("device.list")[0]["transport"] == "loop"
    ctx.close()
