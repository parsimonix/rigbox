"""Exercise continuous RACE reception with deterministic threaded transports and no hardware."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Condition, Event, Lock, get_ident
from time import monotonic

import pytest

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.workspace import Workspace
from rigbox.plugins.device.connection import Connection
from rigbox.plugins.device.protocol import Message
from rigbox.plugins.device.receiver import MessageReceiver
from rigbox.plugins.device.registry import DeviceRegistry
from rigbox.plugins.device.transport import TransportOptions
from rigbox.plugins.device.transports.replay_transport import ReplayTransport


def frame(payload: bytes = b"", *, packet_type: int = 0x5B, command_id: int = 0x0301) -> bytes:
    length = len(payload) + 2
    return bytes((5, packet_type)) + length.to_bytes(2, "little") + command_id.to_bytes(2, "little") + payload


class LiveTransport:
    def __init__(self) -> None:
        self.condition = Condition()
        self.chunks: deque[bytes] = deque()
        self.on_write: bytes = b""
        self.written: list[bytes] = []
        self.readers: set[int] = set()
        self.in_read = Lock()
        self.started = Event()
        self.write_started = Event()
        self.release_write: Event | None = None
        self.failure: OSError | None = None
        self.ended = False
        self.closed = False

    def feed(self, chunk: bytes) -> None:
        with self.condition:
            self.chunks.append(chunk)
            self.condition.notify_all()

    def write(self, data: bytes) -> None:
        self.written.append(data)
        self.write_started.set()
        if self.on_write:
            self.feed(self.on_write)
        if self.release_write is not None:
            if not self.release_write.wait(1):
                raise OSError("test write was not released")

    def read_chunk(self, timeout_s: float) -> bytes:
        assert self.in_read.acquire(blocking=False), "competing readers"
        try:
            self.started.set()
            self.readers.add(get_ident())
            with self.condition:

                def ready() -> bool:
                    return bool(self.chunks) or self.ended or self.failure is not None

                self.condition.wait_for(ready, timeout=timeout_s)
                if self.failure:
                    raise self.failure
                if self.chunks:
                    return self.chunks.popleft()
                if self.ended:
                    raise EOFError
                return b""
        finally:
            self.in_read.release()

    def read(self, size: int, timeout_s: float) -> bytes:
        return self.read_chunk(timeout_s)[:size]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def device() -> Iterator[tuple[Connection, LiveTransport, list[str]]]:
    registry = Registry()
    workspace = Workspace.load(Path(__file__).resolve().parents[1] / "race")
    load_all(registry, workspace)
    transport = LiveTransport()
    logs: list[str] = []
    connection = Connection(
        "dut",
        "serial://test",
        transport,
        TransportOptions(timeout_s=0.3),
        registry=registry.extension(DeviceRegistry),
        log=logs.append,
    )
    try:
        yield connection, transport, logs
    finally:
        connection.close()


def wait_packets(connection: Connection, count: int) -> None:
    deadline = monotonic() + 1
    while monotonic() < deadline:
        if connection.receive_stats()["queued_packets"] >= count:
            return
        Event().wait(0.005)
    pytest.fail("receiver did not queue expected messages")


def test_starts_on_first_framed_operation_and_keeps_receiving_between_commands(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    assert not transport.started.is_set()
    connection.send_message(b"", "race-uart", options={"command_id": "1", "packet_type": "0x5c"})
    assert transport.started.wait(1)
    transport.feed(frame(b"idle", packet_type=0x5D))
    wait_packets(connection, 1)
    assert connection.receive_message("race-uart", 0) == Message(
        b"idle",
        frame(b"idle", packet_type=0x5D),
        {"command_id": 0x0301, "packet_type": 0x5D, "length": 6},
    )
    assert len(transport.readers) == 1


def test_response_bypasses_overflowing_unsolicited_queue(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, logs = device
    notification = frame(b"x", packet_type=0x5D, command_id=0x0F41)
    transport.on_write = notification * 600 + frame(b"SDK")
    response = connection.request_message(b"", "race-uart", 1, options={"command_id": "0x0301"})
    assert response is not None and response.payload == b"SDK"
    assert connection.receive_stats() == {
        "queued_packets": 256,
        "queued_wire_bytes": 256 * len(notification),
        "dropped_packets": 344,
        "dropped_wire_bytes": 344 * len(notification),
    }
    assert any("overflow dropped 344 oldest packets" in text for text in logs)
    assert len(transport.written) == 1


def test_persistent_parser_finishes_old_fragment_without_blocking_request(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    connection.send_message(b"", "race-uart", options={"command_id": "1", "packet_type": "0x5c"})
    notification = frame(b"partial", packet_type=0x5D)
    transport.feed(notification[:4])
    transport.on_write = notification[4:] + frame(b"response")
    response = connection.request_message(b"", "race-uart", 1, options={"command_id": "0x0301"})
    assert response is not None and response.payload == b"response"
    queued = connection.receive_message("race-uart", 1)
    assert queued is not None and queued.wire_data == notification


def test_old_queued_response_does_not_complete_new_request(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    connection.send_message(b"", "race", options={"command_id": "1", "packet_type": "0x5c"})
    transport.feed(frame(b"old"))
    wait_packets(connection, 1)
    transport.on_write = frame(b"new")
    response = connection.request_message(b"", "race", 1, options={"command_id": "0x0301"})
    assert response is not None and response.payload == b"new"
    queued = connection.receive_message("race", 0)
    assert queued is not None and queued.payload == b"old"


def test_filters_remain_request_local_and_matching_response_wins(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, logs = device
    notification = frame(b"log", packet_type=0x5D)
    transport.on_write = notification * 300 + frame(b"ok")
    response = connection.request_message(
        b"",
        "race-uart",
        1,
        options={"command_id": "0x0301", "discard_packets": "0x5d,0x5b"},
    )
    assert response is not None and response.payload == b"ok"
    assert connection.receive_stats()["queued_packets"] == 0
    assert any("filters discarded 300 packets" in text for text in logs)
    transport.feed(notification)
    wait_packets(connection, 1)
    assert connection.receive_message("race-uart", 0) is not None


def test_timeout_and_late_reply_do_not_allow_another_ambiguous_request(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    assert connection.request_message(b"", "race", 0.02, options={"command_id": "0x0301"}) is None
    transport.feed(frame(b"late"))
    wait_packets(connection, 1)
    assert connection.receive_message("race", 0) is not None
    with pytest.raises(CommandError, match="unresolved"):
        connection.request_message(b"", "race", 1, options={"command_id": "0x0301"})
    assert len(transport.written) == 1


def test_close_joins_reader_and_protocol_switch_does_not_lose_input(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    connection.send_message(b"", "race-uart", options={"command_id": "1", "packet_type": "0x5c"})
    transport.feed(frame(b"queued"))
    wait_packets(connection, 1)
    with pytest.raises(CommandError, match="cannot switch"):
        connection.check_protocol("raw")
    assert connection.receive_message("race-uart", 0) is not None
    connection.check_protocol("raw")
    connection.write(b"raw")
    connection.close()
    assert transport.closed
    assert transport.in_read.acquire(blocking=False)
    transport.in_read.release()


def test_worker_io_failure_reaches_foreground(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    connection.send_message(b"", "race", options={"command_id": "1", "packet_type": "0x5c"})
    with transport.condition:
        transport.failure = OSError("disconnected")
        transport.condition.notify_all()
    with pytest.raises(CommandError, match="disconnected"):
        connection.receive_message("race", 1)


def test_eof_drains_queued_frames_and_reports_truncation(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    transport.feed(frame(b"valid") + b"\x05")
    with transport.condition:
        transport.ended = True
    message = connection.receive_message("race", 1)
    assert message is not None and message.payload == b"valid"
    with pytest.raises(CommandError, match="incomplete packet"):
        connection.receive_message("race", 1)


def test_byte_limit_drops_oldest_without_exceeding_memory_bound(
    device: tuple[Connection, LiveTransport, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MessageReceiver, "MAX_BYTES", 14)
    connection, transport, _ = device
    transport.on_write = frame(b"a", packet_type=0x5D) + frame(b"bb", packet_type=0x5D) + frame(b"ok")
    assert connection.request_message(b"", "race", 1, options={"command_id": "0x0301"}) is not None
    stats = connection.receive_stats()
    assert stats["queued_packets"] == 1
    assert stats["queued_wire_bytes"] == 8
    assert stats["dropped_packets"] == 1


def test_receiving_continues_during_write_and_response_deadline_starts_after_it(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    transport.release_write = Event()
    transport.on_write = frame(b"early")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(connection.request_message, b"", "race", 0.01, options={"command_id": "0x0301"})
        try:
            assert transport.write_started.wait(1)
            assert transport.started.wait(1)
            # The response arrives during the write, before its response deadline starts.
            Event().wait(0.03)
        finally:
            transport.release_write.set()
        result = future.result(timeout=1)
    assert result is not None and result.payload == b"early"
    assert len(transport.readers) == 1


def test_request_preserves_exclusive_foreground_transaction_lock(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(connection.request_message, b"", "race", 1, options={"command_id": "0x0301"})
        try:
            assert transport.write_started.wait(1)
            with pytest.raises(CommandError, match="timeout waiting for connection"):
                connection.receive_message("race", 0)
        finally:
            transport.feed(frame(b"reply"))
        assert future.result(timeout=1) is not None


def test_failed_write_is_not_retried_and_keeps_unresolved_state(
    device: tuple[Connection, LiveTransport, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, transport, _ = device

    def fail(data: bytes) -> None:
        transport.written.append(data)
        raise OSError("uncertain write")

    monkeypatch.setattr(transport, "write", fail)
    with pytest.raises(CommandError, match="uncertain write"):
        connection.request_message(b"", "race", 1, options={"command_id": "0x0301"})
    with pytest.raises(CommandError, match="unresolved"):
        connection.send_message(b"", "race", options={"command_id": "0x0301"})
    assert len(transport.written) == 1


def test_idle_overflow_is_observable_and_keeps_newest_messages(
    device: tuple[Connection, LiveTransport, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MessageReceiver, "MAX_PACKETS", 2)
    connection, transport, logs = device
    connection.send_message(b"", "race", options={"command_id": "1", "packet_type": "0x5c"})
    transport.feed(b"".join(frame(bytes((value,)), packet_type=0x5D) for value in range(10)))
    wait_packets(connection, 2)
    assert connection.receive_stats()["dropped_packets"] == 8
    first = connection.receive_message("race", 0)
    last = connection.receive_message("race", 0)
    assert first is not None and first.payload == b"\x08"
    assert last is not None and last.payload == b"\x09"
    assert any("overflow dropped 8 oldest packets" in text for text in logs)


def test_invalid_request_does_not_write_or_make_session_unresolved(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    with pytest.raises(CommandError, match="invalid RACE message"):
        connection.request_message(b"", "race", 1, options={"command_id": "bad"})
    with pytest.raises(CommandError, match="use device.send"):
        connection.request_message(b"", "race", 1, options={"command_id": "1", "packet_type": "0x5c"})
    assert not transport.written
    transport.on_write = frame(b"valid")
    assert connection.request_message(b"", "race", 1, options={"command_id": "0x0301"}) is not None


def test_request_filters_prune_idle_queue_without_persisting(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, logs = device
    connection.send_message(b"", "race", options={"command_id": "1", "packet_type": "0x5c"})
    transport.feed(frame(b"old", packet_type=0x5D))
    wait_packets(connection, 1)
    transport.on_write = frame(b"reply")
    assert (
        connection.request_message(
            b"",
            "race",
            1,
            options={"command_id": "0x0301", "discard_packets": "0x5d"},
        )
        is not None
    )
    assert connection.receive_stats()["queued_packets"] == 0
    assert any("filters discarded 1 packets" in text for text in logs)


def test_split_uart_escape_is_preserved_across_transactions(
    device: tuple[Connection, LiveTransport, list[str]],
) -> None:
    connection, transport, _ = device
    connection.send_message(b"", "race-uart", options={"command_id": "1", "packet_type": "0x5c"})
    notification = frame(b"\x11", packet_type=0x5D)
    transport.feed(notification[:-1] + b"\x77")
    transport.on_write = b"\xee" + frame(b"ok")
    result = connection.request_message(b"", "race-uart", 1, options={"command_id": "0x0301"})
    assert result is not None and result.payload == b"ok"
    queued = connection.receive_message("race-uart", 1)
    assert queued is not None
    assert queued.payload == b"\x11"
    assert queued.wire_data == notification[:-1] + b"\x77\xee"


def test_commands_use_continuous_race_and_capture_preserves_wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = Registry()
    workspace = Workspace.load(Path(__file__).resolve().parents[1] / "race")
    load_all(registry, workspace)
    transport = LiveTransport()

    def factory(url: str, options: TransportOptions) -> LiveTransport:
        return transport

    monkeypatch.setitem(registry.extension(DeviceRegistry).transports, "serial", factory)
    ctx = Context(registry=registry)
    notification = frame(b"\x11", packet_type=0x5D).replace(b"\x11", b"\x77\xee")
    transport.on_write = notification * 300 + frame(b"SDK")
    try:
        ctx.run_line("device.connect dut serial://test --timeout 0.3")
        response = ctx.run_line('device.request dut --protocol race-uart --protocol-option command_id=0x0301 --hex "" --timeout 1 --payload-format ascii')
        assert response.structured_data == {
            "metadata": {"command_id": 0x0301, "packet_type": 0x5B, "length": 5},
            "payload": {"size": 3, "text": "SDK"},
        }
        assert "unsolicited queue overflow dropped 44 oldest packets" in capsys.readouterr().err
        rows = ctx.run("device.list").structured_data
        assert isinstance(rows, list) and rows[0]["receive"]["queued_packets"] == 256
        output = tmp_path / "capture.ndjson"
        ctx.run("device.capture", {"name": "dut", "protocol": "race-uart", "count": 1, "out": str(output)})
        record = json.loads(output.read_text())
        assert record["hex"] == notification.hex()
        assert record["payload_hex"] == "11"
        assert record["protocol"] == "race-uart"
    finally:
        ctx.close()
    assert transport.closed


def test_replay_remains_consumer_paced_without_live_queue_losses(tmp_path: Path) -> None:
    registry = Registry()
    load_all(registry, Workspace.load(Path(__file__).resolve().parents[1] / "race"))
    path = tmp_path / "recording.ndjson"
    path.write_text(
        "\n".join(json.dumps({"hex": frame(value.to_bytes(2, "little")).hex()}) for value in range(300)),
        encoding="utf-8",
    )
    connection = Connection(
        "playback",
        f"replay://{path}",
        ReplayTransport(path),
        TransportOptions(),
        registry=registry.extension(DeviceRegistry),
    )
    try:
        for value in range(300):
            message = connection.receive_message("race", 1)
            assert message is not None and message.payload == value.to_bytes(2, "little")
        assert connection.receive_stats() == {}
        with pytest.raises(EOFError):
            connection.receive_message("race", 1)
    finally:
        connection.close()
