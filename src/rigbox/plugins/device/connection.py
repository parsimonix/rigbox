"""Own a named byte-stream session, fixed-size reads, and serialized requests."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from math import isfinite
from threading import RLock
from time import monotonic
from types import TracebackType

from rigbox.core.registry import CommandError
from rigbox.core.transport import Transport, TransportOptions, scheme_of


class Connection:
    """Own an open transport and serialize access without a background reader."""

    def __init__(self, name: str, url: str, transport: Transport, options: TransportOptions) -> None:
        """Wrap an already opened `transport` (see `Registry.open_transport`)."""
        self.name: str = name
        self.url: str = url
        self.options: TransportOptions = options
        self.transport: Transport = transport
        self._closed: bool = False
        self._lock: RLock = RLock()

    def __enter__(self) -> Connection:
        """Return the open session for use in a context manager."""
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the session without hiding an exception from the managed block."""
        try:
            self.close()
        except CommandError as error:
            if exc_value is None:
                raise
            exc_value.add_note(str(error))

    @property
    def scheme(self) -> str:
        """The URL scheme that selected the transport."""
        return scheme_of(self.url)

    @property
    def timeout(self) -> float:
        """Default read timeout in seconds."""
        return self.options.timeout

    def write(self, data: bytes) -> None:
        """Send `data` to the device."""
        with self._access(self.timeout):
            self._ensure_open()
            self._write(data)

    def read(self, size: int, timeout: float) -> bytes:
        """Collect `size` bytes within one deadline, returning partial data on timeout or EOF."""
        with self._access(timeout) as remaining:
            self._ensure_open()
            data, ended = self._read(size, remaining)
            if ended and not data:
                raise EOFError("end of stream")
            return data

    def read_exactly(self, size: int, timeout: float) -> bytes:
        """Read one fixed-size packet or report a timeout or end of stream with partial data."""
        with self._access(timeout) as remaining:
            self._ensure_open()
            return self._read_exactly(size, remaining)

    def request(self, data: bytes, size: int, timeout: float) -> bytes:
        """Send data and read a fixed-size reply without another caller interleaving I/O.

        The response timeout starts after the write. Lock acquisition is bounded separately by the same timeout;
        the transport is responsible for bounding its write. No queued input is discarded and no write is retried.
        """
        if size < 1:
            raise ValueError("size must be positive")
        with self._access(timeout):
            self._ensure_open()
            self._write(data)
            return self._read_exactly(size, timeout)

    def read_chunk(self, timeout: float) -> bytes:
        """Wait up to `timeout` seconds for data and return what arrived (see `Transport.read_chunk`)."""
        with self._access(timeout) as remaining:
            self._ensure_open()
            try:
                return self.transport.read_chunk(remaining)
            except OSError as error:
                raise CommandError(f"cannot read from '{self.name}': {error}") from error

    def close(self) -> None:
        """Close the owned transport once; allow retrying a failed close."""
        with self._access(self.timeout):
            if self._closed:
                return
            try:
                self.transport.close()
            except OSError as error:
                raise CommandError(f"cannot close '{self.name}': {error}") from error
            self._closed = True

    @contextmanager
    def _access(self, timeout: float) -> Generator[float, None, None]:
        if not isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        deadline = monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise CommandError(f"timeout waiting for connection '{self.name}'")
        try:
            yield max(0.0, deadline - monotonic())
        finally:
            self._lock.release()

    def _ensure_open(self) -> None:
        if self._closed:
            raise CommandError(f"connection '{self.name}' is closed")

    def _write(self, data: bytes) -> None:
        try:
            self.transport.write(data)
        except OSError as error:
            raise CommandError(f"cannot write to '{self.name}': {error}") from error

    def _read(self, size: int, timeout: float) -> tuple[bytes, bool]:
        if size < 1:
            raise ValueError("size must be positive")
        deadline = monotonic() + timeout
        data = bytearray()
        while len(data) < size:
            remaining = max(0.0, deadline - monotonic())
            if data and remaining == 0:
                break
            try:
                chunk = self.transport.read(size - len(data), remaining)
            except EOFError:
                return bytes(data), True
            except OSError as error:
                raise CommandError(f"cannot read from '{self.name}': {error}") from error
            if not chunk:
                break
            if len(chunk) > size - len(data):
                raise CommandError(f"transport for '{self.name}' returned more bytes than requested")
            data.extend(chunk)
        return bytes(data), False

    def _read_exactly(self, size: int, timeout: float) -> bytes:
        data, ended = self._read(size, timeout)
        if len(data) != size:
            reason = "stream ended" if ended else "timeout"
            raise CommandError(f"{reason}: got {len(data)} of {size} bytes ({data.hex()})")
        return data
