"""Serial transport: pyserial ports and its URL handlers (loop://, socket://, rfc2217://)."""

from __future__ import annotations

import time

import serial

from rigbox.core.registry import CommandError
from rigbox.core.transport import Transport, TransportOptions, scheme_of

DEFAULT_BAUD = 115200
MAX_CHUNK_SIZE = 65536


class SerialTransport:
    """Adapt pyserial to a byte stream without framing, decoding, or reader threads."""

    def __init__(self, url: str, options: TransportOptions) -> None:
        """Open `url` (a port name or pyserial URL) with the options' baud rate and timeout.

        Raises:
            CommandError: If the port cannot be opened.
        """
        self._write_timeout: float | None = options.write_timeout if options.write_timeout is not None else options.timeout
        if scheme_of(url) == "rfc2217":
            if options.write_timeout is not None:
                raise CommandError("pyserial's RFC2217 transport does not support --write-timeout")
            self._write_timeout = None
        try:
            self._port = serial.serial_for_url(
                url.removeprefix("serial://"),
                baudrate=options.baud or DEFAULT_BAUD,
                timeout=options.timeout,
                write_timeout=self._write_timeout,
            )
        except (serial.SerialException, ValueError, OSError) as e:
            raise CommandError(f"cannot open {url}: {e}") from None

    def write(self, data: bytes) -> None:
        """Write all bytes within one deadline, without an unbounded flush.

        RFC2217 retains pyserial's own network behavior because that handler cannot set a write timeout.
        """
        if not data:
            return
        if self._write_timeout is None:
            if self._port.write(data) != len(data):
                raise serial.SerialTimeoutException("incomplete write")
            return
        deadline = time.monotonic() + self._write_timeout
        offset = 0
        while offset < len(data):
            remaining = min(self._write_timeout, deadline - time.monotonic())
            if remaining <= 0:
                raise serial.SerialTimeoutException(f"write timeout after {offset} of {len(data)} bytes")
            self._port.write_timeout = remaining
            written = self._port.write(data[offset:])
            if written is None or written <= 0:
                raise serial.SerialTimeoutException(f"write made no progress after {offset} of {len(data)} bytes")
            offset += written

    def read(self, size: int, timeout: float) -> bytes:
        """Read up to `size` bytes; the session layer owns packet assembly and its overall deadline."""
        self._port.timeout = timeout
        return self._port.read(size)

    def read_chunk(self, timeout: float) -> bytes:
        """Wait up to `timeout` for data, then return whatever has arrived."""
        self._port.timeout = timeout
        first = self._port.read(1)
        if not first:
            return b""
        self._port.timeout = 0
        return first + self._port.read(min(self._port.in_waiting, MAX_CHUNK_SIZE - 1))

    def close(self) -> None:
        """Close the port."""
        self._port.close()


def open_serial(url: str, options: TransportOptions) -> Transport:
    """Serial port or pyserial URL: COM3, /dev/ttyUSB0, loop://, socket://host:port, rfc2217://host:port."""
    return SerialTransport(url, options)
