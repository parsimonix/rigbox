"""Serial transport: pyserial ports and its URL handlers (loop://, socket://, rfc2217://)."""

from __future__ import annotations

import time
from typing import ClassVar

import serial

from rigbox.core.registry import CommandError
from rigbox.plugins.device.transport import Transport, TransportOptions, scheme_of


class SerialTransport:
    """Transport that adapts pyserial to a byte stream without framing, decoding or reader threads."""

    DEFAULT_BAUD: ClassVar[int] = 115200
    MAX_CHUNK_SIZE: ClassVar[int] = 65536

    def __init__(self, url: str, options: TransportOptions) -> None:
        """Open `url` (a port name or pyserial URL) with the options' baud rate and timeout.

        Args:
            url (str): The port URL.
            options (TransportOptions): The transport options.

        Raises:
            CommandError: If the port cannot be opened.
        """
        self._write_timeout_s: float | None = options.write_timeout_s if options.write_timeout_s is not None else options.timeout_s
        if scheme_of(url) == "rfc2217":
            if options.write_timeout_s is not None:
                raise CommandError("pyserial's RFC2217 transport does not support --write-timeout")
            self._write_timeout_s = None
        try:
            self._port = serial.serial_for_url(
                url.removeprefix("serial://"),
                baudrate=options.baud or self.DEFAULT_BAUD,
                timeout=options.timeout_s,
                write_timeout=self._write_timeout_s,
            )
        except (serial.SerialException, ValueError, OSError) as e:
            raise CommandError(f"cannot open {url}: {e}") from None

    def write(self, data: bytes) -> None:
        """Write all bytes within one deadline, without an unbounded flush.

        NOTE: RFC2217 retains pyserial's own network behavior because that handler cannot set a write timeout.

        Args:
            data (bytes): The data to write.

        Raises:
            serial.SerialTimeoutException: If the write times out.
        """
        if not data:
            return
        if self._write_timeout_s is None:
            if self._port.write(data) != len(data):
                raise serial.SerialTimeoutException("incomplete write")
            return
        deadline = time.monotonic() + self._write_timeout_s
        offset = 0
        while offset < len(data):
            remaining = min(self._write_timeout_s, deadline - time.monotonic())
            if remaining <= 0:
                raise serial.SerialTimeoutException(f"write timeout after {offset} of {len(data)} bytes")
            self._port.write_timeout = remaining
            written = self._port.write(data[offset:])
            if written is None or written <= 0:
                raise serial.SerialTimeoutException(f"write made no progress after {offset} of {len(data)} bytes")
            offset += written

    def read(self, size: int, timeout_s: float) -> bytes:
        """Read up to `size` bytes; the session layer owns packet assembly and its overall deadline.

        Args:
            size (int): The number of bytes to read.
            timeout_s (float): The read timeout in seconds.

        Returns:
            bytes: The data read.
        """
        self._port.timeout = timeout_s
        return self._port.read(size)

    def read_chunk(self, timeout_s: float) -> bytes:
        """Wait up to `timeout_s` for data, then return whatever has arrived.

        Args:
            timeout_s (float): The maximum time to wait for data, in seconds.

        Returns:
            bytes: The data that was read.
        """
        self._port.timeout = timeout_s
        first = self._port.read(1)
        if not first:
            return b""
        self._port.timeout = 0
        return first + self._port.read(min(self._port.in_waiting, self.MAX_CHUNK_SIZE - 1))

    def close(self) -> None:
        """Close the port."""
        self._port.close()


def open_serial(url: str, options: TransportOptions) -> Transport:
    """Open a serial device transport.

    Examples:
        open_serial("COM3", options)
        open_serial("/dev/ttyUSB0", options)
        open_serial("loop://", options)
        open_serial("rfc2217://host:port", options)
        open_serial("socket://host:port", options)

    Args:
        url (str): The serial port URL.
        options (TransportOptions): Transport options.

    Returns:
        Transport: The opened transport.
    """
    return SerialTransport(url, options)
