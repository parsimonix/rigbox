import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.workspace import Workspace
from rigbox.plugins.device.connection import Connection
from rigbox.plugins.device.transports.serial_transport import SerialTransport

EXAMPLE = str(Path(__file__).parent.parent / "examples" / "sensor_report.yaml")
VALUES = "{id: 258, temperature: 2.5, ok: true, label: node1, samples: [1,-2,3,4], blob: dead}"
DECODED = {
    "id": 258,
    "temperature": 2.5,
    "ok": True,
    "label": "node1",
    "samples": [1, -2, 3, 4],
    "blob": "dead",
}


@pytest.fixture
def ctx() -> Iterator[Context]:
    reg = Registry()
    load_all(reg)
    c = Context(registry=reg)
    c.run("device.connect", {"name": "d", "url": "loop://", "timeout": 0.1})
    yield c
    c.close()


def test_connect_list_close(ctx: Context) -> None:
    assert ctx.run("device.list")[0]["name"] == "d"
    with pytest.raises(CommandError, match="already exists"):
        ctx.run("device.connect", {"name": "d", "url": "loop://"})
    ctx.run("device.close", {"name": "d"})
    assert ctx.run("device.list") == []
    with pytest.raises(CommandError, match="no connection named"):
        ctx.run("device.send", {"name": "d", "hex": "00"})


def test_bad_urls_are_command_errors(ctx: Context) -> None:
    with pytest.raises(CommandError, match="no transport for 'nosuchscheme' URLs"):
        ctx.run("device.connect", {"name": "x", "url": "nosuchscheme://foo"})
    with pytest.raises(CommandError, match="cannot open"):
        ctx.run("device.connect", {"name": "x", "url": "serial://definitely-not-a-port"})
    assert [r["name"] for r in ctx.run("device.list")] == ["d"]


def test_raw_roundtrip_over_loopback(ctx: Context) -> None:
    sent = ctx.run("device.send", {"name": "d", "hex": "de ad be ef"})[0]
    assert sent["sent"] == 4
    assert ctx.run("device.expect", {"name": "d", "size": 4}) == [{"size": 4, "hex": "deadbeef"}]


def test_manifest_roundtrip_over_loopback(ctx: Context) -> None:
    ctx.run("device.send", {"name": "d", "manifest": EXAMPLE, "values": VALUES})
    assert ctx.run("device.expect", {"name": "d", "manifest": EXAMPLE}) == [DECODED]


def test_raw_request_over_loopback(ctx: Context) -> None:
    assert ctx.run("device.request", {"name": "d", "hex": "010203", "response_size": 3}) == [{"size": 3, "hex": "010203"}]


def test_manifest_request_over_loopback(ctx: Context) -> None:
    assert ctx.run(
        "device.request",
        {
            "name": "d",
            "manifest": EXAMPLE,
            "values": VALUES,
            "response_manifest": EXAMPLE,
        },
    ) == [DECODED]


def test_invalid_request_response_is_rejected_before_writing(ctx: Context) -> None:
    with pytest.raises(CommandError, match="exactly one"):
        ctx.run("device.request", {"name": "d", "hex": "0102"})
    with pytest.raises(CommandError, match="timeout: got 0"):
        ctx.run("device.expect", {"name": "d", "size": 1, "timeout": 0.01})


def test_request_timeout_reports_partial_response(ctx: Context) -> None:
    with pytest.raises(CommandError, match=r"timeout: got 2 of 3 bytes \(0102\)"):
        ctx.run(
            "device.request",
            {"name": "d", "hex": "0102", "response_size": 3, "timeout": 0.01},
        )


def test_request_tokens_parse_through_shared_parser(ctx: Context) -> None:
    assert ctx.run_line("device.request d --hex 0102 --response-size 2 --timeout 0.1") == [{"size": 2, "hex": "0102"}]


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), -1.0, 0.0])
def test_connect_rejects_nonfinite_and_nonpositive_write_timeouts(ctx: Context, timeout_s: float) -> None:
    with pytest.raises(CommandError, match="write_timeout"):
        ctx.run(
            "device.connect",
            {"name": "invalid", "url": "loop://", "write_timeout": timeout_s},
        )
    assert [row["name"] for row in ctx.run("device.list")] == ["d"]


def test_connect_accepts_a_separate_write_deadline(ctx: Context) -> None:
    ctx.run(
        "device.connect",
        {"name": "writer", "url": "loop://", "timeout": 0.5, "write_timeout": 0.2},
    )
    assert ctx.run("device.request", {"name": "writer", "hex": "0102", "response_size": 2}) == [{"size": 2, "hex": "0102"}]


def test_request_reports_partial_replay_eof(ctx: Context, tmp_path: Path) -> None:
    path = tmp_path / "reply.ndjson"
    path.write_text('{"dir": "rx", "hex": "0102"}\n', encoding="utf-8")
    ctx.run("device.connect", {"name": "recording", "url": f"replay://{path}"})
    with pytest.raises(CommandError, match=r"stream ended: got 2 of 3 bytes \(0102\)"):
        ctx.run("device.request", {"name": "recording", "hex": "00", "response_size": 3})


def test_expect_times_out_with_partial_data(ctx: Context) -> None:
    ctx.run("device.send", {"name": "d", "hex": "0102"})
    with pytest.raises(CommandError, match=r"timeout: got 2 of 4 bytes \(0102\)"):
        ctx.run("device.expect", {"name": "d", "size": 4, "timeout": 0.05})


def test_argument_rules(ctx: Context) -> None:
    with pytest.raises(CommandError, match="either --hex"):
        ctx.run("device.send", {"name": "d"})
    with pytest.raises(CommandError, match="either --hex"):
        ctx.run(
            "device.send",
            {"name": "d", "hex": "00", "manifest": EXAMPLE, "values": "{}"},
        )
    with pytest.raises(CommandError, match="bad hex"):
        ctx.run("device.send", {"name": "d", "hex": "zz"})
    with pytest.raises(CommandError, match="exactly one"):
        ctx.run("device.expect", {"name": "d"})


def test_capture_decodes_and_records_bad_packets(ctx: Context, tmp_path: Path) -> None:
    for _ in range(2):
        ctx.run("device.send", {"name": "d", "manifest": EXAMPLE, "values": VALUES})
    ctx.run("device.send", {"name": "d", "hex": "010203"})  # trailing partial packet
    out = tmp_path / "cap.ndjson"
    (summary,) = ctx.run(
        "device.capture",
        {"name": "d", "manifest": EXAMPLE, "seconds": 0.3, "out": str(out)},
    )
    assert (summary["packets"], summary["errors"]) == (3, 1)
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["decoded"] for r in records[:2]] == [DECODED, DECODED]
    assert "partial packet (3 of 25 bytes)" in records[2]["error"]
    assert all(r["dir"] == "rx" and r["ts"] for r in records)


def test_capture_stops_at_count_and_raw_mode_goes_to_workspace(tmp_path: Path) -> None:
    ws = Workspace.init(tmp_path / "proj")
    reg = Registry()
    load_all(reg, ws)
    ctx = Context(registry=reg, workspace=ws)
    ctx.run("device.connect", {"name": "d", "url": "loop://", "timeout": 0.1})
    for chunk in ("aa", "bb"):
        ctx.run("device.send", {"name": "d", "hex": chunk})
    (summary,) = ctx.run("device.capture", {"name": "d", "size": 1, "count": 1, "seconds": 5})
    assert summary["packets"] == 1
    assert Path(summary["file"]).parent == ws.captures_dir
    ctx.close()


def test_connections_persist_across_workflow_steps_and_close_at_end(
    tmp_path: Path,
) -> None:
    reg = Registry()
    load_all(reg)
    ctx = Context(registry=reg)
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nsteps:\n"
        "  - {run: device.connect, args: {name: d, url: 'loop://', timeout: 0.1}}\n"
        "  - {run: device.send, args: {name: d, hex: '0a0b'}}\n"
        "  - {run: device.expect, args: {name: d, size: 2}}\n",
        encoding="utf-8",
    )
    assert ctx.run("workflow.run", {"workflow": str(wf)}) == [{"size": 2, "hex": "0a0b"}]
    (conn,) = ctx.connections.values()
    assert isinstance(conn, Connection)
    ctx.close()
    assert isinstance(conn.transport, SerialTransport)
    assert ctx.connections == {} and not conn.transport._port.is_open


def test_ports_lists_without_error(ctx: Context) -> None:
    assert isinstance(ctx.run("device.ports"), list)
