"""Exercise protocol framing, connection ownership and command behavior without hardware."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.render import normalize_structured_data
from rigbox.core.workspace import Workspace
from rigbox.plugins.device import line_protocol
from rigbox.plugins.device.connection import Connection
from rigbox.plugins.device.line_protocol import LineProtocol, open_line
from rigbox.plugins.device.protocol import Message, ProtocolSession
from rigbox.plugins.device.registry import DeviceRegistry
from rigbox.plugins.device.transport import TransportOptions


class ChunkTransport:
    def __init__(self, *chunks: bytes, ended: bool = False) -> None:
        self.chunks: deque[bytes] = deque(chunks)
        self.ended = ended
        self.written: list[bytes] = []
        self.waits: list[float] = []
        self.closed = False
        self.error: OSError | None = None

    def write(self, data: bytes) -> None:
        if self.error:
            raise self.error
        self.written.append(data)

    def read(self, size: int, timeout: float) -> bytes:
        chunk = self.read_chunk(timeout)
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
        return chunk[:size]

    def read_chunk(self, timeout: float) -> bytes:
        self.waits.append(timeout)
        if self.error:
            raise self.error
        if self.chunks:
            return self.chunks.popleft()
        if self.ended:
            raise EOFError
        return b""

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def ctx() -> Iterator[Context]:
    registry = Registry()
    load_all(registry)
    context = Context(registry=registry)
    try:
        context.run("device.connect", {"name": "d", "url": "loop://", "timeout": 0.02})
        yield context
    finally:
        context.close()


def run(
    ctx: Context, command: str, args: dict[str, object] | None = None
) -> list[dict[str, Any]]:
    # Command results are dynamically shaped rows, normalized through the public rendering helper.
    return normalize_structured_data(ctx.run(command, args).structured_data)


def connect_stub(ctx: Context, transport: ChunkTransport) -> Connection:
    connection = Connection(
        "stub",
        "stub://",
        transport,
        TransportOptions(timeout=0.1),
        registry=ctx.registry.extension(DeviceRegistry),
    )
    ctx.connections["stub"] = connection
    return connection


def test_protocol_discovery_and_raw_default(ctx: Context) -> None:
    rows = {row["protocol"]: row["description"] for row in run(ctx, "device.protocols")}
    assert set(rows) == {"raw", "line"}
    assert "LF" in rows["line"]
    assert run(
        ctx, "device.request", {"name": "d", "text": "hi", "response_size": 2}
    ) == [{"size": 2, "hex": "6869"}]
    assert run(
        ctx,
        "device.request",
        {"name": "d", "protocol": "RAW", "hex": "ff", "response_size": 1},
    ) == [{"size": 1, "hex": "ff"}]


def test_line_send_expect_and_request_use_shared_parser(ctx: Context) -> None:
    assert normalize_structured_data(
        ctx.run_line("device.send d --protocol LINE --text hi").structured_data
    ) == [{"name": "d", "protocol": "line", "sent": 3, "hex": "68690a"}]
    assert ctx.run_line("device.expect d --protocol line").structured_data == {
        "size": 2,
        "hex": "6869",
    }
    assert ctx.run_line(
        "device.request d --protocol line --text status"
    ).structured_data == {"size": 6, "hex": "737461747573"}


def test_empty_message_is_not_a_timeout(ctx: Context, tmp_path: Path) -> None:
    assert run(
        ctx, "device.request", {"name": "d", "protocol": "line", "text": ""}
    ) == [{"size": 0, "hex": ""}]
    ctx.run("device.send", {"name": "d", "protocol": "line", "text": ""})
    out = tmp_path / "empty.ndjson"
    summary = run(
        ctx,
        "device.capture",
        {"name": "d", "protocol": "line", "count": 1, "out": str(out)},
    )[0]
    assert summary["packets"] == 1
    assert json.loads(out.read_text())["hex"] == "0a"


def test_line_retains_fragmented_coalesced_and_cr_input() -> None:
    transport = ChunkTransport(b"hel", b"lo\r\nworld\n\npartial")
    session = LineProtocol()
    assert session.receive(transport, 1) == Message(b"hello\r", b"hello\r\n")
    assert session.receive(transport, 0) == Message(b"world", b"world\n")
    assert session.receive(transport, 0) == Message(b"", b"\n")
    assert session.receive(transport, 0) is None
    assert session.pending_input
    transport.chunks.append(b" rest\n")
    assert session.receive(transport, 1) == Message(b"partial rest", b"partial rest\n")
    assert not session.pending_input


def test_partial_timeout_is_retained_and_prevents_raw_access(ctx: Context) -> None:
    transport = ChunkTransport(b"part")
    connection = connect_stub(ctx, transport)
    with pytest.raises(CommandError, match="timeout.*partial input is retained"):
        ctx.run("device.expect", {"name": "stub", "protocol": "line"})
    with pytest.raises(CommandError, match="cannot switch.*buffered input"):
        connection.write(b"raw")
    assert transport.written == []
    transport.chunks.append(b"ial\n")
    assert run(ctx, "device.expect", {"name": "stub", "protocol": "line"}) == [
        {"size": 7, "hex": b"partial".hex()}
    ]
    connection.write(b"raw")
    assert transport.written == [b"raw"]


@pytest.mark.parametrize(
    "command", ["device.send", "device.expect", "device.request", "device.capture"]
)
def test_all_raw_commands_respect_pending_protocol_input(
    ctx: Context, tmp_path: Path, command: str
) -> None:
    connection = connect_stub(ctx, ChunkTransport(b"first\nsecond\n"))
    assert connection.receive_message("line", 1) == Message(b"first", b"first\n")
    args: dict[str, object] = {"name": "stub"}
    if command in ("device.send", "device.request"):
        args["hex"] = "01"
    if command == "device.request":
        args["response_size"] = 1
    if command == "device.expect":
        args["size"] = 1
    path = tmp_path / "blocked.ndjson"
    if command == "device.capture":
        args["out"] = str(path)
    with pytest.raises(CommandError, match="cannot switch"):
        ctx.run(command, args)
    assert not path.exists()
    assert connection.receive_message("line", 0) == Message(b"second", b"second\n")


def test_sessions_are_per_connection_and_factory_is_reused(ctx: Context) -> None:
    created: list[LineProtocol] = []
    schemes: list[str] = []

    def factory(scheme: str) -> ProtocolSession:
        session = LineProtocol()
        created.append(session)
        schemes.append(scheme)
        return session

    devices = ctx.registry.extension(DeviceRegistry)
    devices.add_protocol("custom", factory)
    a = connect_stub(ctx, ChunkTransport(b"a", b""))
    b = Connection(
        "other", "stub://", ChunkTransport(b"b\n"), TransportOptions(), registry=devices
    )
    ctx.connections["other"] = b
    assert a.receive_message("custom", 1) is None
    assert b.receive_message("custom", 1) == Message(b"b", b"b\n")
    assert a.receive_message("custom", 0) is None
    assert (
        len(created) == 2 and created[0].pending_input and not created[1].pending_input
    )
    assert schemes == ["stub", "stub"]


def test_switch_to_other_protocol_requires_draining_known_input(ctx: Context) -> None:
    ctx.registry.extension(DeviceRegistry).add_protocol("other", open_line)
    connection = connect_stub(ctx, ChunkTransport(b"a\nb\n"))
    assert connection.receive_message("line", 1) == Message(b"a", b"a\n")
    with pytest.raises(CommandError, match="cannot switch.*other"):
        connection.send_message(b"query", "other")
    assert connection.receive_message("line", 0) == Message(b"b", b"b\n")
    assert connection.send_message(b"query", "other") == b"query\n"


def test_line_request_does_not_clear_queued_messages(ctx: Context) -> None:
    connection = connect_stub(ctx, ChunkTransport(b"old\nnew\n"))
    assert connection.request_message(b"query", "line", 1) == Message(b"old", b"old\n")
    assert connection.receive_message("line", 0) == Message(b"new", b"new\n")


def test_line_uses_one_overall_read_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = 0.0

    def clock() -> float:
        return elapsed

    class TimedTransport(ChunkTransport):
        def read_chunk(self, timeout: float) -> bytes:
            nonlocal elapsed
            elapsed += min(timeout, 0.4)
            return super().read_chunk(timeout)

    monkeypatch.setattr(line_protocol, "monotonic", clock)
    transport = TimedTransport(b"a", b"b", b"c")
    assert LineProtocol().receive(transport, 1) is None
    assert transport.waits == pytest.approx([1, 0.6, 0.2])
    assert elapsed == pytest.approx(1)


def test_zero_timeout_polls_and_returns_complete_buffered_messages() -> None:
    transport = ChunkTransport(b"a\nb\n")
    session = LineProtocol()
    assert session.receive(transport, 0) == Message(b"a", b"a\n")
    assert session.receive(transport, 0) == Message(b"b", b"b\n")
    assert transport.waits == [0]


def test_exact_line_buffer_limit_and_overflow_are_explicit() -> None:
    maximum = LineProtocol.MAX_BUFFER_SIZE
    session = LineProtocol()
    wire = session.encode(b"x" * (maximum - 1))
    assert len(wire) == maximum
    assert session.receive(ChunkTransport(wire), 1) == Message(wire[:-1], wire)
    with pytest.raises(CommandError, match="exceeds"):
        session.encode(b"x" * maximum)
    for chunk in (b"x" * maximum, b"x" * (maximum + 1)):
        session = LineProtocol()
        with pytest.raises(CommandError, match="close and reconnect"):
            session.receive(ChunkTransport(chunk), 1)
        assert session.pending_input
        with pytest.raises(CommandError, match="session failed"):
            session.encode(b"later")
        with pytest.raises(CommandError, match="session failed"):
            session.receive(ChunkTransport(), 0)


def test_eof_does_not_turn_incomplete_input_into_a_message(ctx: Context) -> None:
    transport = ChunkTransport(b"ok\npartial", ended=True)
    connection = connect_stub(ctx, transport)
    assert connection.receive_message("line", 1) == Message(b"ok", b"ok\n")
    with pytest.raises(CommandError, match="incomplete line.*7 bytes"):
        connection.receive_message("line", 1)
    with pytest.raises(CommandError, match="cannot switch"):
        connection.read_chunk(0)
    connection.close()
    assert transport.closed


def test_clean_eof_is_user_facing_for_expect_and_request(ctx: Context) -> None:
    connect_stub(ctx, ChunkTransport(ended=True))
    with pytest.raises(CommandError, match="stream ended waiting"):
        ctx.run("device.expect", {"name": "stub", "protocol": "line"})
    with pytest.raises(CommandError, match="stream ended waiting"):
        ctx.run("device.request", {"name": "stub", "protocol": "line", "text": "query"})


def test_bad_selection_or_payload_is_rejected_before_writing(ctx: Context) -> None:
    transport = ChunkTransport()
    connect_stub(ctx, transport)
    for args in (
        {"protocol": "missing", "text": "hello"},
        {"protocol": "", "text": "hello"},
        {"protocol": "line", "text": "hello\nthere"},
        {"protocol": "line", "text": "hello", "response_size": 1},
        {"protocol": "line", "text": "hello", "hex": "ff"},
    ):
        with pytest.raises(CommandError):
            ctx.run("device.request", {"name": "stub", **args})
    assert transport.written == [] and transport.waits == []


def test_protocol_io_failures_are_user_facing_without_retry(ctx: Context) -> None:
    transport = ChunkTransport()
    connection = connect_stub(ctx, transport)
    transport.error = OSError("unplugged")
    with pytest.raises(CommandError, match="cannot read.*unplugged"):
        connection.receive_message("line", 1)
    with pytest.raises(CommandError, match="cannot write.*unplugged"):
        connection.send_message(b"x", "line")
    transport.waits.clear()
    with pytest.raises(CommandError, match="communication failed.*unplugged"):
        connection.request_message(b"x", "line", 1)
    assert transport.waits == [] and transport.written == []


def test_request_owns_connection_until_response_finishes(ctx: Context) -> None:
    reading, release = Event(), Event()

    class BlockingTransport(ChunkTransport):
        def read_chunk(self, timeout: float) -> bytes:
            reading.set()
            if not release.wait(2):
                raise OSError("test reader was not released")
            return super().read_chunk(timeout)

    transport = BlockingTransport(b"reply\n")
    connection = connect_stub(ctx, transport)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(connection.request_message, b"query", "line", 1)
        try:
            assert reading.wait(1)
            with pytest.raises(CommandError, match="timeout waiting for connection"):
                connection.receive_message("line", 0)
            assert transport.written == [b"query\n"]
        finally:
            release.set()
        assert result.result(timeout=2) == Message(b"reply", b"reply\n")


def test_protocol_registration_compatibility_and_factory_errors(ctx: Context) -> None:
    devices = ctx.registry.extension(DeviceRegistry)
    with pytest.raises(ValueError, match="reserved"):
        devices.add_protocol("RAW", open_line)
    with pytest.raises(ValueError, match="duplicate"):
        devices.add_protocol("LINE", open_line)
    with pytest.raises(ValueError, match="empty"):
        devices.add_protocol(" ", open_line)

    def incompatible(scheme: str) -> ProtocolSession:
        raise CommandError(f"not supported over {scheme}")

    def broken(scheme: str) -> ProtocolSession:
        raise RuntimeError("broken factory")

    devices.add_protocol("incompatible", incompatible)
    devices.add_protocol("broken", broken)
    for name, error in (
        ("incompatible", "not supported over loop"),
        ("broken", "cannot create.*broken factory"),
    ):
        with pytest.raises(CommandError, match=error):
            ctx.run("device.send", {"name": "d", "protocol": name, "text": "hi"})


def test_capture_preserves_wire_bytes_and_replays_protocol_messages(
    ctx: Context, tmp_path: Path
) -> None:
    connect_stub(ctx, ChunkTransport(b"a\n\npar", b"t\n", ended=True))
    path = tmp_path / "lines.ndjson"
    summary = run(
        ctx, "device.capture", {"name": "stub", "protocol": "line", "out": str(path)}
    )[0]
    assert (summary["packets"], summary["errors"]) == (3, 0)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["hex"] for row in rows] == ["610a", "0a", "706172740a"]
    assert [row["payload_hex"] for row in rows] == ["61", "", "70617274"]
    assert all(row["protocol"] == "line" for row in rows)
    ctx.run("device.connect", {"name": "r", "url": f"replay://{path}"})
    for payload in ("61", "", "70617274"):
        assert (
            run(ctx, "device.expect", {"name": "r", "protocol": "line"})[0]["hex"]
            == payload
        )


def test_protocol_manifest_decodes_payload_not_wire_bytes(
    ctx: Context, tmp_path: Path
) -> None:
    path = tmp_path / "byte.yaml"
    path.write_text(
        "name: byte\nfields:\n  - {name: value, type: u8}\n", encoding="utf-8"
    )
    assert run(
        ctx,
        "device.request",
        {
            "name": "d",
            "protocol": "line",
            "manifest": str(path),
            "values": "{value: 42}",
            "response_manifest": str(path),
        },
    ) == [{"value": 42}]
    ctx.run("device.send", {"name": "d", "protocol": "line", "hex": "2b"})
    capture = tmp_path / "decoded.ndjson"
    ctx.run(
        "device.capture",
        {
            "name": "d",
            "protocol": "line",
            "manifest": str(path),
            "count": 1,
            "out": str(capture),
        },
    )
    assert json.loads(capture.read_text())["decoded"] == {"value": 43}


def test_capture_count_retains_extra_messages_for_next_operation(
    ctx: Context, tmp_path: Path
) -> None:
    connect_stub(ctx, ChunkTransport(b"first\nsecond\n"))
    path = tmp_path / "one.ndjson"
    summary = run(
        ctx,
        "device.capture",
        {"name": "stub", "protocol": "line", "count": 1, "out": str(path)},
    )[0]
    assert summary["packets"] == 1
    assert run(ctx, "device.expect", {"name": "stub", "protocol": "line"}) == [
        {"size": 6, "hex": b"second".hex()}
    ]


def test_capture_records_payload_decode_errors_with_wire_evidence(
    ctx: Context, tmp_path: Path
) -> None:
    connect_stub(ctx, ChunkTransport(b"\x01\n", ended=True))
    manifest = tmp_path / "pair.yaml"
    manifest.write_text(
        "name: pair\nfields:\n  - {name: value, type: u16}\n", encoding="utf-8"
    )
    out = tmp_path / "bad.ndjson"
    summary = run(
        ctx,
        "device.capture",
        {
            "name": "stub",
            "protocol": "line",
            "manifest": str(manifest),
            "out": str(out),
        },
    )[0]
    assert (summary["packets"], summary["errors"]) == (1, 1)
    record = json.loads(out.read_text())
    assert record["hex"] == "010a" and record["payload_hex"] == "01"
    assert record["error"] == "expected 2 bytes, got 1"


def test_request_response_budget_starts_after_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = 0.0

    def clock() -> float:
        return elapsed

    class SlowWriter(ChunkTransport):
        def write(self, data: bytes) -> None:
            nonlocal elapsed
            elapsed += 0.8
            super().write(data)

    monkeypatch.setattr(line_protocol, "monotonic", clock)
    transport = SlowWriter(b"reply\n")
    assert LineProtocol().request(transport, b"query", 1) == Message(
        b"reply", b"reply\n"
    )
    assert transport.waits == [1]


def test_workspace_plugin_can_register_a_protocol(tmp_path: Path) -> None:
    workspace = Workspace.init(tmp_path / "project")
    (workspace.plugins_dir / "lines.py").write_text(
        "from rigbox.hooks import hookimpl\n"
        "from rigbox.plugins.device.line_protocol import open_line\n"
        "from rigbox.plugins.device.registry import DeviceRegistry\n"
        "@hookimpl\n"
        "def rigbox_register(registry):\n"
        "    registry.extension(DeviceRegistry).add_protocol('custom-line', open_line)\n",
        encoding="utf-8",
    )
    registry = Registry()
    load_all(registry, workspace)
    context = Context(registry=registry, workspace=workspace)
    try:
        assert "custom-line" in {
            row["protocol"] for row in run(context, "device.protocols")
        }
        context.run("device.connect", {"name": "d", "url": "loop://"})
        assert run(
            context,
            "device.request",
            {"name": "d", "protocol": "custom-line", "text": "ok"},
        ) == [{"size": 2, "hex": "6f6b"}]
    finally:
        context.close()
