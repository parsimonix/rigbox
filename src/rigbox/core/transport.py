"""Transports: how rigbox talks to a device.

A transport is a plain byte stream. The registry maps a URL scheme (``loop``, ``replay``, ...) to a factory that opens
one, so a plugin can add support for a new kind of device by registering a scheme (see `Registry.add_transport`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_SCHEME = "serial"
"""The scheme assumed for a URL with no ``://``, such as ``COM3`` or ``/dev/ttyUSB0``."""


@dataclass(frozen=True)
class TransportOptions:
    """Settings handed to a transport factory when a connection is opened."""

    timeout: float = 1.0
    """Default read timeout in seconds."""

    baud: int | None = None
    """Serial baud rate; transports without one ignore it."""

    params: Mapping[str, str] = field(default_factory=dict)
    """Extra transport-specific ``key=value`` options (``device.connect --param key=value``)."""


class Transport(Protocol):
    """A byte stream to a device. Any object with these methods works; no base class is needed."""

    def write(self, data: bytes) -> None:
        """Send `data` to the device."""
        ...

    def read(self, size: int, timeout: float) -> bytes:
        """Read up to `size` bytes, giving up after `timeout` seconds; returns fewer bytes on timeout.

        Raises:
            EOFError: If the stream has ended for good (such as a finished recording) and nothing was read.
                A live device never ends, so it just times out.
        """
        ...

    def read_chunk(self, timeout: float) -> bytes:
        """Wait up to `timeout` seconds for data and return whatever has arrived (empty on timeout).

        Raises:
            EOFError: If the stream has ended for good.
        """
        ...

    def close(self) -> None:
        """Release the underlying resource."""
        ...


TransportFactory = Callable[[str, TransportOptions], Transport]
"""Opens a transport from a URL. Raise `CommandError` for a user-facing failure. The first line of its docstring
is shown by ``device.transports``."""


def scheme_of(url: str) -> str:
    """Return the lower-cased scheme of `url`, or `DEFAULT_SCHEME` when it has no ``://``."""
    scheme, separator, _ = url.partition("://")
    return scheme.lower() if separator else DEFAULT_SCHEME
