"""A named device connection: a transport plus the settings it was opened with."""

from __future__ import annotations

from rigbox.core.transport import Transport, TransportOptions, scheme_of


class Connection:
    """A named, open connection. Reads and writes go to the underlying `Transport`."""

    def __init__(self, name: str, url: str, transport: Transport, options: TransportOptions) -> None:
        """Wrap an already opened `transport` (see `Registry.open_transport`)."""
        self.name = name
        self.url = url
        self.options = options
        self.transport = transport

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
        self.transport.write(data)

    def read(self, size: int, timeout: float) -> bytes:
        """Read up to `size` bytes within `timeout` seconds (see `Transport.read`)."""
        return self.transport.read(size, timeout)

    def read_chunk(self, timeout: float) -> bytes:
        """Wait up to `timeout` seconds for data and return what arrived (see `Transport.read_chunk`)."""
        return self.transport.read_chunk(timeout)

    def close(self) -> None:
        """Close the transport."""
        self.transport.close()
