"""Test byte-stream sessions without plugin discovery or physical hardware."""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from rigbox.core.context import Context
from rigbox.core.registry import CommandError, Registry
from rigbox.plugins.device import connection as connection_module
from rigbox.plugins.device.connection import Connection
from rigbox.plugins.device.transport import TransportOptions
from rigbox.plugins.device.transports import serial_transport as serial_module
from rigbox.plugins.device.transports.serial_transport import SerialTransport


class StubTransport:
    """Provide controllable partial reads and I/O failures."""

    def __init__(self, chunks: tuple[bytes, ...] = (), *, ended: bool = False) -> None:
        """Initialize queued chunks and the eventual end-of-stream behavior."""
        self.chunks: deque[bytes] = deque(chunks)
        self.ended: bool = ended
        self.written: list[bytes] = []
        self.waits: list[float] = []
        self.closed: int = 0
        self.read_error: OSError | None = None
        self.write_error: OSError | None = None
        self.close_error: OSError | None = None

    def write(self, data: bytes) -> None:
        """Record a write or raise the configured failure."""
        if self.write_error is not None:
            raise self.write_error
        self.written.append(data)

    def read(self, size: int, timeout: float) -> bytes:
        """Return at most one queued chunk, respecting the requested size."""
        self.waits.append(timeout)
        if self.read_error is not None:
            raise self.read_error
        if not self.chunks:
            if self.ended:
                raise EOFError("end of recording")
            return b""
        chunk = self.chunks.popleft()
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
        return chunk[:size]

    def read_chunk(self, timeout: float) -> bytes:
        """Return the next available chunk."""
        return self.read(4096, timeout)

    def close(self) -> None:
        """Count cleanup attempts and optionally fail."""
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def connection(transport: StubTransport) -> Connection:
    return Connection("device", "stub://", transport, TransportOptions(timeout=0.5))


def test_session_collects_partial_reads_into_one_fixed_size_packet() -> None:
    transport = StubTransport((b"a", b"bc", b"de"))
    session = connection(transport)
    assert session.read(4, 0.5) == b"abcd"
    assert session.read(1, 0.5) == b"e"


def test_session_reports_backend_io_failures_as_command_errors() -> None:
    transport = StubTransport()
    transport.read_error = OSError("device unplugged")
    with pytest.raises(CommandError, match="device unplugged"):
        connection(transport).read(1, 0.5)


def test_session_cleanup_is_idempotent() -> None:
    transport = StubTransport()
    session = connection(transport)
    session.close()
    session.close()
    assert transport.closed == 1


@pytest.mark.parametrize("ended, reason", [(False, "timeout"), (True, "stream ended")])
def test_exact_read_reports_partial_bytes_and_distinguishes_eof(ended: bool, reason: str) -> None:
    session = connection(StubTransport((b"a", b"b"), ended=ended))
    with pytest.raises(CommandError, match=rf"{reason}: got 2 of 3 bytes \(6162\)"):
        session.read_exactly(3, 0.5)


def test_partial_eof_is_returned_for_capture_then_eof_is_reported() -> None:
    session = connection(StubTransport((b"a",), ended=True))
    assert session.read(2, 0.5) == b"a"
    with pytest.raises(EOFError):
        session.read(1, 0.5)
    with pytest.raises(EOFError):
        session.read_chunk(0.5)


def test_partial_reads_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = 0.0

    def clock() -> float:
        return elapsed

    class TimedTransport(StubTransport):
        def read(self, size: int, timeout: float) -> bytes:
            nonlocal elapsed
            if timeout < 0.4:
                self.waits.append(timeout)
                elapsed += timeout
                return b""
            elapsed += 0.4
            return super().read(size, timeout)

    monkeypatch.setattr(connection_module, "monotonic", clock)
    transport = TimedTransport((b"a", b"b", b"c"))
    assert connection(transport).read(3, 1.0) == b"ab"
    assert transport.waits == pytest.approx([1.0, 0.6, 0.2])
    assert elapsed == pytest.approx(1.0)


def test_zero_timeout_polls_without_waiting() -> None:
    transport = StubTransport((b"abc",))
    assert connection(transport).read(3, 0) == b"abc"
    assert transport.waits == [0]


@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("inf")])
def test_invalid_timeouts_are_rejected_before_io(timeout: float) -> None:
    transport = StubTransport((b"a",))
    with pytest.raises(ValueError, match="timeout"):
        connection(transport).request(b"query", 1, timeout)
    assert transport.written == []


def test_request_keeps_input_and_collects_one_fixed_size_response() -> None:
    transport = StubTransport((b"a", b"bc", b"later"))
    session = connection(transport)
    assert session.request(b"query", 3, 0.5) == b"abc"
    assert transport.written == [b"query"]
    assert session.read_chunk(0.5) == b"later"


def test_request_holds_the_session_lock_through_reading() -> None:
    reading = Event()
    release = Event()

    class BlockingTransport(StubTransport):
        def read(self, size: int, timeout: float) -> bytes:
            reading.set()
            if not release.wait(2):
                raise OSError("test did not release reader")
            return super().read(size, timeout)

    transport = BlockingTransport((b"reply",))
    session = connection(transport)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.request, b"query", 5, 0.5)
        try:
            assert reading.wait(1)
            with pytest.raises(CommandError, match="timeout waiting for connection"):
                session.read_chunk(0)
            assert transport.written == [b"query"]
        finally:
            release.set()
        assert future.result(timeout=2) == b"reply"


def test_failed_write_is_not_retried_or_followed_by_a_read() -> None:
    transport = StubTransport()
    transport.write_error = OSError("write failed")
    with pytest.raises(CommandError, match="cannot write.*write failed"):
        connection(transport).request(b"query", 1, 0.5)
    assert transport.waits == []


def test_chunk_io_errors_are_user_facing() -> None:
    transport = StubTransport()
    transport.read_error = OSError("unplugged")
    with pytest.raises(CommandError, match="cannot read.*unplugged"):
        connection(transport).read_chunk(0.5)


def test_closed_session_rejects_io_and_context_entry() -> None:
    session = connection(StubTransport())
    session.close()
    with pytest.raises(CommandError, match="is closed"):
        session.write(b"x")
    with pytest.raises(CommandError, match="is closed"):
        session.read(1, 0.5)
    with pytest.raises(CommandError, match="is closed"):
        with session:
            pytest.fail("closed session entered")


def test_failed_close_can_be_retried() -> None:
    transport = StubTransport()
    transport.close_error = OSError("close failed")
    session = connection(transport)
    with pytest.raises(CommandError, match="cannot close.*close failed"):
        session.close()
    transport.close_error = None
    session.close()
    assert transport.closed == 2


def test_context_manager_closes_and_preserves_the_original_error() -> None:
    transport = StubTransport()
    with pytest.raises(RuntimeError, match="original") as captured:
        with connection(transport):
            transport.close_error = OSError("close failed")
            raise RuntimeError("original")
    assert transport.closed == 1
    assert "close failed" in captured.value.__notes__[0]


def test_loopback_request_and_context_managed_cleanup() -> None:
    options = TransportOptions(timeout=0.1)
    transport = SerialTransport("loop://", options)
    with Connection("loop", "loop://", transport, options) as session:
        assert session.request(b"hello", 5, 0.1) == b"hello"
    assert not transport._port.is_open


def test_serial_handles_partial_writes_without_flushing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = bytearray()
    settings: dict[str, object] = {}

    class PartialPort:
        write_timeout: float = 0.0

        def write(self, data: bytes) -> int:
            written.extend(data[:1])
            return 1

        def flush(self) -> None:
            pytest.fail("write must not use an unbounded flush")

    port = PartialPort()

    def open_port(url: str, **kwargs: object) -> PartialPort:
        settings.update(kwargs)
        return port

    monkeypatch.setattr(serial_module.serial, "serial_for_url", open_port)
    SerialTransport("serial://COM3", TransportOptions(timeout=0.5, write_timeout=0.2)).write(b"abc")
    assert written == b"abc"
    assert settings["write_timeout"] == 0.2
    assert 0 < port.write_timeout <= 0.2


def test_rfc2217_rejects_an_explicit_unsupported_write_timeout() -> None:
    with pytest.raises(CommandError, match="RFC2217.*does not support --write-timeout"):
        SerialTransport("rfc2217://unused:1234", TransportOptions(write_timeout=0.5))


def test_context_cleanup_attempts_every_resource_and_reports_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = StubTransport()
    bad.close_error = OSError("cleanup failed")
    good = StubTransport()
    ctx = Context(registry=Registry(), connections={"bad": bad, "good": good})
    ctx.close()
    ctx.close()
    assert bad.closed == good.closed == 1
    assert ctx.connections == {}
    output = capsys.readouterr()
    assert output.out == ""
    assert "cannot close connection 'bad': cleanup failed" in output.err


@pytest.mark.parametrize("progress", [None, 0])
def test_serial_rejects_writes_that_make_no_progress(progress: int | None, monkeypatch: pytest.MonkeyPatch) -> None:
    class StalledPort:
        write_timeout: float = 0.0

        def write(self, data: bytes) -> int | None:
            return progress

    def open_port(url: str, **kwargs: object) -> StalledPort:
        return StalledPort()

    monkeypatch.setattr(serial_module.serial, "serial_for_url", open_port)
    options = TransportOptions(timeout=0.1)
    session = Connection("serial", "COM3", SerialTransport("COM3", options), options)
    with pytest.raises(CommandError, match="cannot write.*no progress"):
        session.write(b"abc")


def test_serial_partial_writes_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = 0.0
    waits: list[float] = []

    def clock() -> float:
        return elapsed

    class SlowPort:
        write_timeout: float = 0.0

        def write(self, data: bytes) -> int:
            nonlocal elapsed
            waits.append(self.write_timeout)
            elapsed += 0.3
            return 1

    def open_port(url: str, **kwargs: object) -> SlowPort:
        return SlowPort()

    monkeypatch.setattr(serial_module, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(serial_module.serial, "serial_for_url", open_port)
    options = TransportOptions(timeout=0.5)
    session = Connection("serial", "COM3", SerialTransport("COM3", options), options)
    with pytest.raises(CommandError, match="write timeout after 2 of 3 bytes"):
        session.write(b"abc")
    assert waits == pytest.approx([0.5, 0.2])


def test_serial_raw_chunks_are_bounded_and_only_wait_for_the_first_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[tuple[int, float]] = []

    class BufferedPort:
        timeout: float = 0.0
        in_waiting: int = 1000000

        def read(self, size: int) -> bytes:
            reads.append((size, self.timeout))
            return b"x" * size

    def open_port(url: str, **kwargs: object) -> BufferedPort:
        return BufferedPort()

    monkeypatch.setattr(serial_module.serial, "serial_for_url", open_port)
    transport = SerialTransport("COM3", TransportOptions())
    assert len(transport.read_chunk(0.5)) == serial_module.MAX_CHUNK_SIZE
    assert reads == [(1, 0.5), (serial_module.MAX_CHUNK_SIZE - 1, 0)]


def test_request_rejects_invalid_size_before_writing() -> None:
    transport = StubTransport()
    with pytest.raises(ValueError, match="size must be positive"):
        connection(transport).request(b"query", 0, 0.5)
    assert transport.written == []
