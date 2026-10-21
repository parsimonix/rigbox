"""Transports: how rigbox talks to a device.

A transport is a plain byte stream. The registry maps a URL scheme (``loop``, ``replay``, ...) to a factory that opens
one, so a plugin can add device support by registering a scheme (see `DeviceRegistry.add_transport`).
Framing, decoding and request/response coordination belong above this interface in a `ProtocolSession`. Report-based transports must
explicitly adapt report identifiers and boundaries rather than silently discarding them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

DEFAULT_SCHEME: Final[str] = "serial"
"""The scheme assumed for a URL with no ``://``, such as ``COM3`` or ``/dev/ttyUSB0``."""


@dataclass(frozen=True)
class TransportOptions:
    """Settings handed to a transport factory when a connection is opened."""

    timeout_s: float = 1.0
    """Default read timeout in seconds."""

    baud: int | None = None
    """Serial baud rate; transports without one ignore it."""

    params: Mapping[str, str] = field(default_factory=dict)
    """Extra transport-specific ``key=value`` options (``device.connect --param key=value``)."""

    write_timeout_s: float | None = None
    """Write deadline in seconds where supported; serial defaults to ``timeout`` (RFC2217 is an exception)."""


class Transport(Protocol):
    """A byte stream to a device. Any object with these methods works; no base class is needed."""

    def write(self, data: bytes) -> None:
        """Send all of `data` or raise `OSError`; never silently accept a short write.

        Implementations must document write-timeout limitations. Success means accepted by the transport, not
        acknowledged by the remote device. Do not retry a failed command automatically.

        Args:
            data (bytes): The data to write.

        Raises:
            OSError: If the write fails.
        """
        ...

    def read(self, size: int, timeout_s: float) -> bytes:
        """Read at most `size` bytes within `timeout_s` seconds; partial reads are allowed and zero timeout polls.

        Args:
            size (int): Maximum number of bytes to read.
            timeout_s (float): Read timeout in seconds.

        Raises:
            EOFError: If the stream has ended for good (such as a finished recording) and nothing was read.
            OSError: If communication fails or the device disconnects unexpectedly.
        """
        ...

    def read_chunk(self, timeout_s: float) -> bytes:
        """Wait up to `timeout_s` seconds for data and return whatever has arrived (empty on timeout).

        Args:
            timeout_s (float): Maximum time in seconds to wait for data.

        Raises:
            EOFError: If the stream has ended for good.
            OSError: If communication fails or the device disconnects unexpectedly.
        """
        ...

    def close(self) -> None:
        """Release the underlying resource; repeated calls should be idempotent."""
        ...


TransportFactory = Callable[[str, TransportOptions], Transport]
"""Opens a transport from a URL. Raise `CommandError` for a user-facing failure. The first line of its docstring
is shown by ``device.transports``."""


def scheme_of(url: str) -> str:
    """Return the lower-cased scheme of `url`, or `DEFAULT_SCHEME` when it has no ``://``.

    Args:
        url (str): The URL to extract the scheme from.

    Returns:
        str: The lower-cased scheme of the URL.
    """
    scheme, separator, _ = url.partition("://")
    return scheme.lower() if separator else DEFAULT_SCHEME
