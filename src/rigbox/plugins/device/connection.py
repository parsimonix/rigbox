"""Own a transport, raw fixed-size operations and per-operation message protocol state."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from math import isfinite
from threading import RLock
from time import monotonic
from types import TracebackType
from typing import Self
from warnings import warn

from rigbox.core.registry import CommandError
from rigbox.plugins.device.protocol import Message, ProtocolDiagnostics, ProtocolSession
from rigbox.plugins.device.registry import DeviceRegistry
from rigbox.plugins.device.transport import Transport, TransportOptions, scheme_of


class Connection:
    """Own an open transport and coordinate raw and protocol I/O without a background reader."""

    def __init__(
        self,
        name: str,
        url: str,
        transport: Transport,
        options: TransportOptions,
        *,
        registry: DeviceRegistry | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        """Wrap an already opened transport and optionally enable registered message protocols.

        Args:
            name: Connection identity.
            url: URL that selected the transport.
            transport: Already opened byte stream, owned and closed by this connection.
            options: Default I/O settings.
            registry: Protocol factories; omit only for raw-only Python connections.
            log: Diagnostic sink; Python callers without one receive runtime warnings.
        """
        self.name: str = name
        self.url: str = url
        self.options: TransportOptions = options
        self.transport: Transport = transport
        self._closed: bool = False
        self._lock: RLock = RLock()
        self._registry = registry
        self._protocol_name: str = "raw"
        self._session: ProtocolSession | None = None
        self._log = log

    def __enter__(self) -> Self:
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
            self._select_protocol("raw")
            self._write(data)

    def read(self, size: int, timeout: float) -> bytes:
        """Collect `size` bytes within one deadline, returning partial data on timeout or EOF."""
        with self._access(timeout) as remaining:
            self._ensure_open()
            self._select_protocol("raw")
            data, ended = self._read(size, remaining)
            if ended and not data:
                raise EOFError("end of stream")
            return data

    def read_exactly(self, size: int, timeout: float) -> bytes:
        """Read one fixed-size packet or report a timeout or end of stream with partial data."""
        with self._access(timeout) as remaining:
            self._ensure_open()
            self._select_protocol("raw")
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
            self._select_protocol("raw")
            self._write(data)
            return self._read_exactly(size, timeout)

    def read_chunk(self, timeout: float) -> bytes:
        """Wait up to `timeout` seconds for data and return what arrived (see `Transport.read_chunk`)."""
        with self._access(timeout) as remaining:
            self._ensure_open()
            self._select_protocol("raw")
            try:
                return self.transport.read_chunk(remaining)
            except OSError as error:
                raise CommandError(
                    f"cannot read from '{self.name}': {error}"
                ) from error

    def check_protocol(self, name: str) -> None:
        """Validate protocol selection before command side effects, without reading or writing.

        Args:
            name: Protocol identifier; raw selects existing unframed access.

        Raises:
            CommandError: If selection is unknown, incompatible or would abandon buffered input.
        """
        with self._access(self.timeout):
            self._ensure_open()
            self._select_protocol(name)

    def send_message(
        self, payload: bytes, protocol: str, *, options: Mapping[str, str] | None = None
    ) -> bytes:
        """Frame and send a payload once under the connection lock.

        Args:
            payload: Application bytes without protocol framing.
            protocol: Registered message protocol identifier.
            options: Protocol-specific per-message fields.

        Returns:
            The wire bytes accepted by the transport.

        Raises:
            CommandError: If protocol selection, framing or writing fails.
        """
        with self._access(self.timeout):
            self._ensure_open()
            session = self._message_session(protocol)
            wire = (
                session.encode(payload, options=options)
                if options
                else session.encode(payload)
            )
            self._write(wire)
            return wire

    def receive_message(self, protocol: str, timeout: float) -> Message | None:
        """Receive a message while preserving protocol state between calls.

        Args:
            protocol: Registered message protocol identifier.
            timeout: Overall timeout including lock acquisition, in seconds.

        Returns:
            One complete message, or None on timeout.

        Raises:
            CommandError: If protocol selection, framing or reading fails.
            EOFError: If the stream ends cleanly.
        """
        with self._access(timeout) as remaining:
            self._ensure_open()
            session = self._message_session(protocol)
            try:
                return session.receive(self.transport, remaining)
            except OSError as error:
                raise CommandError(
                    f"cannot read from '{self.name}': {error}"
                ) from error
            finally:
                self._report_diagnostics(session)

    def request_message(
        self,
        payload: bytes,
        protocol: str,
        timeout: float,
        *,
        options: Mapping[str, str] | None = None,
    ) -> Message | None:
        """Serialize a complete protocol request and response without automatic retries.

        Args:
            payload: Application request bytes.
            protocol: Registered message protocol identifier.
            timeout: Response timeout after writing; lock acquisition is bounded separately.
            options: Protocol-specific per-message fields.

        Returns:
            The response message, or None on timeout.

        Raises:
            CommandError: If protocol selection, framing or communication fails.
            EOFError: If the stream ends cleanly before a response arrives.
        """
        with self._access(timeout):
            self._ensure_open()
            session = self._message_session(protocol)
            try:
                if options:
                    return session.request(
                        self.transport, payload, timeout, options=options
                    )
                return session.request(self.transport, payload, timeout)
            except OSError as error:
                raise CommandError(
                    f"communication failed on '{self.name}': {error}"
                ) from error
            finally:
                self._report_diagnostics(session)

    def _report_diagnostics(self, session: ProtocolSession) -> None:
        if isinstance(session, ProtocolDiagnostics):
            for message in session.drain_diagnostics():
                text = f"{self.name}: {message}"
                if self._log is not None:
                    self._log(text)
                else:
                    warn(text, RuntimeWarning, stacklevel=3)

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

    def _select_protocol(self, name: str) -> ProtocolSession | None:
        name = name.strip().lower()
        if name == self._protocol_name:
            return self._session
        if self._session is not None and self._session.pending_input:
            raise CommandError(
                f"cannot switch '{self.name}' from '{self._protocol_name}' to '{name}': "
                "buffered input or a framing failure remains; drain with the same protocol or close and reconnect"
            )
        if name == "raw":
            session = None
        elif self._registry is None:
            raise CommandError("message protocols require a connection with a registry")
        else:
            session = self._registry.create_protocol(name, self.scheme)
        self._session = session
        self._protocol_name = name
        return session

    def _message_session(self, name: str) -> ProtocolSession:
        session = self._select_protocol(name)
        if session is None:
            raise CommandError("use raw connection methods for the raw protocol")
        return session

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
                raise CommandError(
                    f"cannot read from '{self.name}': {error}"
                ) from error
            if not chunk:
                break
            if len(chunk) > size - len(data):
                raise CommandError(
                    f"transport for '{self.name}' returned more bytes than requested"
                )
            data.extend(chunk)
        return bytes(data), False

    def _read_exactly(self, size: int, timeout: float) -> bytes:
        data, ended = self._read(size, timeout)
        if len(data) != size:
            reason = "stream ended" if ended else "timeout"
            raise CommandError(
                f"{reason}: got {len(data)} of {size} bytes ({data.hex()})"
            )
        return data
