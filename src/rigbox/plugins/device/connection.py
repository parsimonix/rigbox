"""Connection: An actively owned transport to a device.

Connections manage an open transport, coordinate per-operation protocol state and provide raw I/O methods.
Protocols with continuous request planning use one connection-owned receiver; other protocols remain synchronous.
"""

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
from rigbox.plugins.device.protocol import (
    ContinuousProtocol,
    FramedRequestProtocol,
    Message,
    ProtocolDiagnostics,
    ProtocolSession,
)
from rigbox.plugins.device.receiver import MessageReceiver
from rigbox.plugins.device.registry import DeviceRegistry
from rigbox.plugins.device.transport import Transport, TransportOptions, scheme_of


class Connection:
    """An actively owned transport to a device."""

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
        """Wrap an already opened transport and optionally enable registered message protocols over that transport.

        Args:
            name (str): The connection name.
            url (str): The connection URL that selected the transport.
            transport (Transport): The transport to wrap, which will be owned and closed by this connection.
            options (TransportOptions): The transport I/O options.
            registry (DeviceRegistry | None, optional): Registry of protocol factories; omit only for raw-only Python connections. Defaults to None.
            log (Callable[[str], None] | None, optional): Diagnostic log; Python callers without one receive runtime warnings. Defaults to None.
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
        self._receiver: MessageReceiver | None = None
        self._log = log

    def __enter__(self) -> Self:
        """Return the open connection for use in a context manager.

        Example:
            ```python
            with conn:
                ...
            ```

        Returns:
            Self: The open connection.
        """
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Handler for context manager exit.

        Closes the session without hiding an exception from the managed block.

        Args:
            exc_type (type[BaseException] | None): The exception type if raised.
            exc_value (BaseException | None): The exception instance if raised.
            traceback (TracebackType | None): The traceback if an exception was raised.
        """
        try:
            self.close()
        except CommandError as error:
            if exc_value is None:
                raise
            exc_value.add_note(str(error))

    @property
    def scheme(self) -> str:
        """Get the URL scheme that selected the transport.

        Returns:
            str: The URL scheme that selected the transport.
        """
        return scheme_of(self.url)

    @property
    def timeout_s(self) -> float:
        """Get the default read timeout in seconds.

        Returns:
            float: The default read timeout in seconds.
        """
        return self.options.timeout_s

    def write(self, data: bytes) -> None:
        """Send `data` to the device.

        Args:
            data (bytes): The data to send.
        """
        with self._access(self.timeout_s):
            self._ensure_open()
            self._select_protocol("raw")
            self._write(data)

    def read(self, size: int, timeout_s: float) -> bytes:
        """Collect `size` bytes within a single deadline of `timeout_s`, returning partial data on timeout or EOF.

        Args:
            size (int): The number of bytes to read.
            timeout_s (float): The read timeout in seconds.

        Raises:
            EOFError: If the end of the stream is reached before reading any data.

        Returns:
            bytes: The data read.
        """
        with self._access(timeout_s) as remaining:
            self._ensure_open()
            self._select_protocol("raw")
            data, ended = self._read(size, remaining)
            if ended and not data:
                raise EOFError("end of stream")
            return data

    def read_exactly(self, size: int, timeout_s: float) -> bytes:
        """Read a single, fixed-size packet or report a timeout or end of stream with partial data.

        Args:
            size (int): The number of bytes to read.
            timeout_s (float): The read timeout in seconds.

        Returns:
            bytes: The data read.
        """
        with self._access(timeout_s) as remaining:
            self._ensure_open()
            self._select_protocol("raw")
            return self._read_exactly(size, remaining)

    def request(self, data: bytes, size: int, timeout_s: float) -> bytes:
        """Send `data` and read a fixed-size reply while preventing concurrent interleaving I/O.

        The response timeout starts after the write is complete. Lock acquisition is bounded separately by the same timeout;
        the transport is responsible for bounding its write. No queued input is discarded and no write is retried.

        Args:
            data (bytes): The request data.
            size (int): The number of bytes to read.
            timeout_s (float): The response timeout in seconds.

        Raises:
            ValueError: If `size` is not positive.

        Returns:
            bytes: The response data.
        """
        if size < 1:
            raise ValueError("size must be positive")
        with self._access(timeout_s):
            self._ensure_open()
            self._select_protocol("raw")
            self._write(data)
            return self._read_exactly(size, timeout_s)

    def read_chunk(self, timeout_s: float) -> bytes:
        """Wait up to `timeout_s` seconds for data and return what arrived (see `Transport.read_chunk`).

        Args:
            timeout_s (float): The maximum time to wait for data in seconds.

        Raises:
            CommandError: If reading fails.

        Returns:
            bytes: The data read.
        """
        with self._access(timeout_s) as remaining:
            self._ensure_open()
            self._select_protocol("raw")
            try:
                return self.transport.read_chunk(remaining)
            except OSError as error:
                raise CommandError(f"cannot read from '{self.name}': {error}") from error

    def check_protocol(self, name: str) -> None:
        """Validate protocol selection before command side effects, without reading or writing.

        Args:
            name (str): The name of the protocol; raw selects existing unframed access.

        Raises:
            CommandError: If selection is unknown, incompatible or would abandon buffered input.
        """
        with self._access(self.timeout_s):
            self._ensure_open()
            self._select_protocol(name)

    def send_message(
        self,
        payload: bytes,
        protocol: str,
        *,
        options: Mapping[str, str] | None = None,
    ) -> bytes:
        """Frame and send a payload once under the connection lock.

        Args:
            payload (bytes): Application bytes without protocol framing.
            protocol (str): Registered message protocol identifier.
            options (Mapping[str, str] | None, optional): Protocol-specific per-message fields. Defaults to None.

        Raises:
            CommandError: If protocol selection, framing or writing fails.

        Returns:
            bytes: The wire bytes sent.
        """
        with self._access(self.timeout_s):
            self._ensure_open()
            session = self._message_session(protocol)
            self._start_receiver(session)
            try:
                if self._receiver is not None:
                    wire = self._receiver.encode(payload, options)
                else:
                    wire = session.encode(payload, options=options) if options else session.encode(payload)
                self._write(wire)
                return wire
            finally:
                self._report_diagnostics(session)

    def receive_message(self, protocol: str, timeout_s: float) -> Message | None:
        """Receive a message while preserving protocol state between calls.

        Args:
            protocol (str): The protocol identifier.
            timeout_s (float): Overall timeout in seconds.

        Raises:
            CommandError: If protocol selection, framing or reading fails.
            EOFError: If the stream ends cleanly.

        Returns:
            Message | None: The received message or None on timeout.
        """
        with self._access(timeout_s) as remaining:
            self._ensure_open()
            session = self._message_session(protocol)
            self._start_receiver(session)
            try:
                if self._receiver is not None:
                    return self._receiver.receive(remaining)
                return session.receive(self.transport, remaining)
            except OSError as error:
                raise CommandError(f"cannot read from '{self.name}': {error}") from error
            finally:
                self._report_diagnostics(session)

    def request_message(
        self,
        payload: bytes,
        protocol: str,
        timeout_s: float,
        *,
        options: Mapping[str, str] | None = None,
        framed: bool = False,
    ) -> Message | None:
        """Serialize a complete protocol request and response without automatic retries.

        Args:
            payload (bytes): The request payload, or a complete unescaped frame when framed is True.
            protocol (str): The protocol identifier.
            timeout_s (float): Response timeout in seconds.
            options (Mapping[str, str] | None, optional): Protocol-specific per-message fields. Defaults to None.
            framed (bool): Validate a complete frame and derive request fields through the protocol.

        Raises:
            CommandError: If protocol selection, framing or communication fails.
            EOFError: If the stream ends cleanly before a response arrives.

        Returns:
            Message | None: The response message or None on timeout.
        """
        with self._access(timeout_s):
            self._ensure_open()
            session = self._message_session(protocol)
            if framed:
                if not isinstance(session, FramedRequestProtocol):
                    raise CommandError(f"protocol '{protocol}' does not support complete request frames")
                payload, options = session.decode_request_frame(payload, options=options)
            self._start_receiver(session)
            try:
                if self._receiver is not None:
                    return self._receiver.request(payload, timeout_s, options)
                if options:
                    return session.request(self.transport, payload, timeout_s, options=options)
                return session.request(self.transport, payload, timeout_s)
            except OSError as error:
                raise CommandError(f"communication failed on '{self.name}': {error}") from error
            finally:
                self._report_diagnostics(session)

    def _report_diagnostics(self, session: ProtocolSession) -> None:
        """Log diagnostic data from `session` to the diagnostic logging callback, if available.

        If no callback is available, a UserWarning will be thrown with the diagnostic data.

        Args:
            session (ProtocolSession): The session to report diagnostics from.
        """
        source = self._receiver if self._receiver is not None else session
        if isinstance(source, ProtocolDiagnostics):
            for message in source.drain_diagnostics():
                text = f"{self.name}: {message}"
                if self._log is not None:
                    self._log(text)
                else:
                    warn(text, RuntimeWarning, stacklevel=3)

    def receive_stats(self) -> dict[str, int]:
        """Report continuous-reception queue occupancy and cumulative losses.

        Returns:
            Queue and overflow counters, or an empty mapping for synchronous sessions.
        """
        with self._access(self.timeout_s):
            if self._receiver is None:
                return {}
            if self._session is not None:
                self._report_diagnostics(self._session)
            return self._receiver.stats()

    def _start_receiver(self, session: ProtocolSession) -> None:
        # Finite replay input needs consumer-driven pacing, not a live-stream overflow policy.
        if self.scheme != "replay" and self._receiver is None and isinstance(session, ContinuousProtocol):
            self._receiver = MessageReceiver(self.transport, session, session)
            self._receiver.start()

    def close(self) -> None:
        """Close the owned transport once; allow retrying a failed close.

        Raises:
            CommandError: If closing the transport fails.
        """
        with self._access(self.timeout_s):
            if self._closed:
                return
            if self._receiver is not None:
                self._receiver.stop()
                if self._session is not None:
                    self._report_diagnostics(self._session)
            try:
                self.transport.close()
            except OSError as error:
                raise CommandError(f"cannot close '{self.name}': {error}") from error
            self._closed = True

    @contextmanager
    def _access(self, timeout_s: float) -> Generator[float, None, None]:
        """Context manager which guarantees locked access to the connection/transport for `timeout_s` seconds.

        The lock is automatically released upon exit.

        Args:
            timeout_s (float): Timeout in seconds.

        Raises:
            ValueError: If timeout_s is not finite or negative
            CommandError: If the lock cannot be acquired within timeout_s

        Yields:
            Generator[float, None, None]: Remaining time in seconds
        """
        if not isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("timeout must be finite and nonnegative")
        deadline = monotonic() + timeout_s
        if not self._lock.acquire(timeout=timeout_s):
            raise CommandError(f"timeout waiting for connection '{self.name}'")
        try:
            yield max(0.0, deadline - monotonic())
        finally:
            self._lock.release()

    def _ensure_open(self) -> None:
        if self._closed:
            raise CommandError(f"connection '{self.name}' is closed")

    def _select_protocol(self, name: str) -> ProtocolSession | None:
        """Create a ProtocolSession associated with the given `name` to be used for an individual operation.

        Args:
            name (str): The protocol name.

        Raises:
            CommandError: If switching protocols fails.
            CommandError: If there is pending input in the current session.

        Returns:
            ProtocolSession | None: The created session or None for raw protocol.
        """
        name = name.strip().lower()
        if name == self._protocol_name:
            return self._session
        receiver = self._receiver
        if receiver is not None:
            receiver.stop()
        pending = receiver.pending_input if receiver is not None else self._session is not None and self._session.pending_input
        if pending:
            if receiver is not None:
                receiver.start()
            raise CommandError(
                f"cannot switch '{self.name}' from '{self._protocol_name}' to '{name}': "
                "buffered input or a framing failure remains; drain with the same protocol or close and reconnect"
            )
        try:
            if name == "raw":
                session = None
            elif self._registry is None:
                raise CommandError("message protocols require a connection with a registry")
            else:
                session = self._registry.create_protocol(name, self.scheme)
        except Exception:
            if receiver is not None:
                receiver.start()
            raise
        if self._session is not None:
            self._report_diagnostics(self._session)
        self._receiver = None
        self._session = session
        self._protocol_name = name
        return session

    def _message_session(self, name: str) -> ProtocolSession:
        session = self._select_protocol(name)
        if session is None:
            raise CommandError("use raw connection methods for the raw protocol")
        return session

    def _write(self, data: bytes) -> None:
        """Write `data` to the current transport.

        Args:
            data (bytes): The data to write.

        Raises:
            CommandError: If writing fails.
        """
        try:
            self.transport.write(data)
        except OSError as error:
            raise CommandError(f"cannot write to '{self.name}': {error}") from error

    def _read(self, size: int, timeout_s: float) -> tuple[bytes, bool]:
        """Read `size` bytes from the current transport.

        Args:
            size (int): The number of bytes to read.
            timeout_s (float): The read timeout in seconds.

        Raises:
            ValueError: If `size` is not positive.
            CommandError: If reading fails.
            CommandError: If the transport returns more bytes than requested.

        Returns:
            tuple[bytes, bool]: The read data and a flag indicating if the stream ended.
        """
        if size < 1:
            raise ValueError("size must be positive")
        deadline = monotonic() + timeout_s
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

    def _read_exactly(self, size: int, timeout_s: float) -> bytes:
        """Read exactly `size` bytes within `timeout_s` seconds, enforcing an error if a complete result is not read.

        Args:
            size (int): The number of bytes to read.
            timeout_s (float): The read timeout in seconds.

        Raises:
            CommandError: If reading exactly `size` bytes fails.

        Returns:
            bytes: The read data.
        """
        data, ended = self._read(size, timeout_s)
        if len(data) != size:
            reason = "stream ended" if ended else "timeout"
            raise CommandError(f"{reason}: got {len(data)} of {size} bytes ({data.hex()})")
        return data
