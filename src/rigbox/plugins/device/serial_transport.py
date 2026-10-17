"""Serial transport: pyserial ports and its URL handlers (loop://, socket://, rfc2217://)."""

from __future__ import annotations

import time

import serial

from rigbox.core.registry import CommandError
from rigbox.core.transport import Transport, TransportOptions

DEFAULT_BAUD = 115200


class SerialTransport:
    """A pyserial port."""

    def __init__(self, url: str, options: TransportOptions) -> None:
        """Open `url` (a port name or pyserial URL) with the options' baud rate and timeout.

        Raises:
            CommandError: If the port cannot be opened.
        """
        try:
            self._port = serial.serial_for_url(
                url.removeprefix("serial://"), baudrate=options.baud or DEFAULT_BAUD, timeout=options.timeout
            )
        except (serial.SerialException, ValueError, OSError) as e:
            raise CommandError(f"cannot open {url}: {e}") from None

    def write(self, data: bytes) -> None:
        """Write `data` and flush."""
        self._port.write(data)
        self._port.flush()

    def read(self, size: int, timeout: float) -> bytes:
        """Read up to `size` bytes, giving up after `timeout` seconds. Returns fewer bytes on timeout."""
        end = time.monotonic() + timeout
        buf = b""
        while len(buf) < size:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self._port.timeout = remaining
            buf += self._port.read(size - len(buf))
        return buf

    def read_chunk(self, timeout: float) -> bytes:
        """Wait up to `timeout` for data, then return whatever has arrived."""
        self._port.timeout = timeout
        first = self._port.read(1)
        return first + self._port.read(self._port.in_waiting) if first else b""

    def close(self) -> None:
        """Close the port."""
        self._port.close()


def open_serial(url: str, options: TransportOptions) -> Transport:
    """Serial port or pyserial URL: COM3, /dev/ttyUSB0, loop://, socket://host:port, rfc2217://host:port."""
    return SerialTransport(url, options)
