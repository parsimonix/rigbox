"""ProtocolSession: Message framing and request coordination for a connection over a transport.

ProtocolSessions exist independently of the transport itself, and multiple protocol sessions can share the same transport.

Factories create one session at a time for a connection. Sessions retain incomplete input and implement message boundaries and request completion;
they neither open nor close the underlying channel.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class MessageChannel(Protocol):
    """Interface to expose I/O from a Transport to a ProtocolSession while the Connection holds its exclusive access lock."""

    def write(self, data: bytes) -> None:
        """Write all bytes or raise an I/O error.

        Args:
            data (bytes): Bytes to transmit without automatic retries.

        Raises:
            OSError: If the write fails, including a short write.
        """
        ...

    def read_chunk(self, timeout_s: float) -> bytes:
        """Read available bytes, returning empty bytes on timeout.

        Args:
            timeout_s (float): Maximum wait in seconds; zero polls.

        Returns:
            Received bytes, whose boundaries need not match message boundaries.

        Raises:
            EOFError: If the channel has permanently ended.
            OSError: If communication fails.
        """
        ...


@dataclass(frozen=True)
class Message:
    """A complete received protocol message."""

    payload: bytes
    wire_data: bytes
    metadata: Mapping[str, int | str] = field(default_factory=dict)


class ProtocolSession(Protocol):
    """Formal definition of a messaging format. This frames messages and coordinates requests for one connection without owning its underlying transport."""

    @property
    def pending_input(self) -> bool:
        """Report whether switching away from this ProtocolSession would abandon received data or a framing failure.

        Returns:
            bool: True when this session must be drained or the connection closed before switching.
        """
        ...

    def encode(self, payload: bytes, *, options: Mapping[str, str] | None = None) -> bytes:
        """Frame one outgoing payload.

        Args:
            payload (bytes): Encoded application data.
            options (Mapping[str, str] | None): Per-message protocol fields, validated before writing.

        Returns:
            bytes: Bytes to write to the channel.

        Raises:
            CommandError: If the payload cannot be framed.
        """
        ...

    def receive(self, channel: MessageChannel, timeout_s: float) -> Message | None:
        """Receive one message within one overall deadline, retaining unfinished or extra input.

        Args:
            channel (MessageChannel): Exclusively held connection I/O.
            timeout_s (float): Maximum wait in seconds; zero polls.

        Returns:
            A complete message, including an empty payload, or None on timeout.

        Raises:
            EOFError: If the stream ended cleanly with no buffered input.
            CommandError: If framing fails or EOF leaves an incomplete message.
            OSError: If communication fails.
        """
        ...

    def request(
        self,
        channel: MessageChannel,
        payload: bytes,
        timeout_s: float,
        *,
        options: Mapping[str, str] | None = None,
    ) -> Message | None:
        """Send once and wait for the protocol's response without discarding unrelated input.

        Args:
            channel (MessageChannel): Exclusively held connection I/O.
            payload (bytes): Encoded application request.
            timeout_s (float): Response deadline in seconds, starting after the write.
            options (Mapping[str, str] | None): Per-message protocol fields, validated before writing.

        Returns:
            Message | None: The response message, or None on timeout.

        Raises:
            CommandError: If framing or response validation fails.
            EOFError: If the stream ends cleanly before a response arrives.
            OSError: If communication fails.
        """
        ...


ProtocolFactory = Callable[[str], ProtocolSession]
"""Create a session for a transport scheme, rejecting unsupported combinations with CommandError."""


@runtime_checkable
class ProtocolDiagnostics(Protocol):
    """Expose recoverable protocol warnings without coupling sessions to command output."""

    def drain_diagnostics(self) -> list[str]:
        """Return and clear pending diagnostics.

        Returns:
            list[str]: Diagnostic messages in occurrence order.
        """
        ...
