"""Frame LF-delimited messages with bounded retained input and no reader thread.

The delimiter is added on send and excluded from received payloads. CR bytes are ordinary payload bytes;
the protocol does not infer prompts, correlate replies, or automatically retry commands.
"""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic
from typing import ClassVar

from rigbox.core.registry import CommandError
from rigbox.plugins.device.protocol import Message, MessageChannel, ProtocolSession


class LineProtocol:
    """Retain partial and coalesced LF-delimited messages for one connection."""

    MAX_BUFFER_SIZE: ClassVar[int] = 65536

    def __init__(self) -> None:
        """Create an empty line session."""
        self._buffer = bytearray()
        self._failed: bool = False

    @property
    def pending_input(self) -> bool:
        """Report whether unread bytes or a framing failure prevent a protocol switch.

        Returns:
            True when buffered input or an overflow failure remains.
        """
        return bool(self._buffer) or self._failed

    def encode(self, payload: bytes, *, options: Mapping[str, str] | None = None) -> bytes:
        """Append LF to one payload, rejecting embedded delimiters and oversized messages.

        Args:
            payload: One message without its terminating LF.
            options: Must be empty; line has no per-message options.

        Returns:
            The LF-terminated wire message.

        Raises:
            CommandError: If the payload contains LF or exceeds the buffer limit.
        """
        self._ensure_healthy()
        if options:
            raise CommandError("line protocol does not accept per-message options")
        if b"\n" in payload:
            raise CommandError("line payload must not contain LF; send one message at a time")
        if len(payload) >= self.MAX_BUFFER_SIZE:
            raise CommandError(f"line message exceeds {self.MAX_BUFFER_SIZE} bytes including LF")
        return payload + b"\n"

    def receive(self, channel: MessageChannel, timeout_s: float) -> Message | None:
        """Receive one line, keeping partial input on timeout and extra lines for subsequent calls.

        Args:
            channel: Exclusively held connection I/O.
            timeout_s: Overall read deadline in seconds.

        Returns:
            A complete message, or None on timeout.

        Raises:
            CommandError: If the input buffer overflows or EOF truncates a message.
            EOFError: If the stream ends with no pending message.
            OSError: If the channel fails.
        """
        self._ensure_healthy()
        deadline = monotonic() + timeout_s
        polled = False
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                wire = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                return Message(payload=wire[:-1], wire_data=wire)
            remaining = max(0.0, deadline - monotonic())
            if polled and remaining == 0:
                return None
            try:
                chunk = channel.read_chunk(remaining)
            except EOFError:
                if self._buffer:
                    raise CommandError(f"stream ended with an incomplete line ({len(self._buffer)} bytes buffered)") from None
                raise
            polled = True
            if not chunk:
                return None
            if len(self._buffer) + len(chunk) > self.MAX_BUFFER_SIZE:
                self._failed = True
                raise CommandError(f"line input exceeds {self.MAX_BUFFER_SIZE} buffered bytes; close and reconnect")
            self._buffer.extend(chunk)
            if len(self._buffer) == self.MAX_BUFFER_SIZE and b"\n" not in self._buffer:
                self._failed = True
                raise CommandError(f"line input exceeds {self.MAX_BUFFER_SIZE} bytes including LF; close and reconnect")

    def request(self, channel: MessageChannel, payload: bytes, timeout_s: float, *, options: Mapping[str, str] | None = None) -> Message | None:
        """Write one line and return the next line, without response correlation or input clearing.

        Args:
            channel: Exclusively held connection I/O.
            payload: Request without its terminating LF.
            timeout_s: Response deadline in seconds, starting after the write.
            options: Must be empty; line has no per-message options.

        Returns:
            The next message, or None on timeout.

        Raises:
            CommandError: If framing fails or EOF truncates a message.
            EOFError: If the channel ends before any message is available.
            OSError: If communication fails.
        """
        channel.write(self.encode(payload, options=options))
        return self.receive(channel, timeout_s)

    def _ensure_healthy(self) -> None:
        if self._failed:
            raise CommandError("line session failed after input overflow; close and reconnect")


def open_line(scheme: str) -> ProtocolSession:
    """Exchange LF-delimited messages over a byte-stream transport.

    Args:
        scheme: Transport scheme; all existing byte-stream transports are supported.

    Returns:
        A fresh line session.
    """
    return LineProtocol()
