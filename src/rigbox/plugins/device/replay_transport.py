"""Replay transport: play a `device.capture` file back as if a device were sending it."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from rigbox.core.registry import CommandError
from rigbox.core.transport import Transport, TransportOptions


class ReplayTransport:
    """Transport that replays received bytes recorded into a capture file; writes are kept in `written` and otherwise ignored."""

    def __init__(self, path: Path) -> None:
        """Load the received (``dir: rx``) records of the NDJSON capture at `path`.

        Args:
            path (Path): The path to the capture file.

        Raises:
            CommandError: If the capture file cannot be read.
        """
        chunks: list[bytes] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    if record.get("dir", "rx") == "rx" and isinstance(
                        record.get("hex"), str
                    ):
                        chunks.append(bytes.fromhex(record["hex"]))
        except (OSError, ValueError, AttributeError) as e:
            raise CommandError(f"cannot read capture {path}: {e}") from None
        self._chunks = deque(chunks)
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        """Write `data` to an internal buffer; a replay recording cannot respond to it.

        Args:
            data (bytes): The data to write.
        """
        self.written.append(data)

    def read(self, size: int, timeout_s: float) -> bytes:
        """Return up to `size` of the remaining recorded bytes at once, ignoring `timeout_s`.

        Args:
            size (int): The number of bytes to read.
            timeout_s (float): The read timeout in seconds (ignored).

        Raises:
            EOFError: If no more data is available.

        Returns:
            bytes: The data read.
        """
        if not self._chunks:
            raise EOFError("end of capture")
        out = bytearray()
        while len(out) < size and self._chunks:
            chunk = self._chunks[0]
            needed = size - len(out)
            out += chunk[:needed]
            if len(chunk) > needed:
                self._chunks[0] = chunk[needed:]
            else:
                self._chunks.popleft()
        return bytes(out)

    def read_chunk(self, timeout_s: float) -> bytes:
        """Return the next recorded packet as it was captured, ignoring `timeout_s`.

        Args:
            timeout_s (float): The maximum time to wait for the next packet (ignored).

        Raises:
            EOFError: If the recording is exhausted.

        Returns:
            bytes: The next recorded packet.
        """
        if not self._chunks:
            raise EOFError("end of capture")
        return self._chunks.popleft()

    def close(self) -> None:
        """Discard what is left of the recording."""
        self._chunks.clear()


def open_replay(url: str, options: TransportOptions) -> Transport:
    """Open a playback transport to play back a capture file: replay://path/to/capture.ndjson.

    Args:
        url (str): The replay URL.
        options (TransportOptions): The transport options.

    Raises:
        CommandError: If the capture file cannot be opened.

    Returns:
        Transport: The opened replay transport.
    """
    path = Path(url.partition("://")[2])
    if not path.is_file():
        raise CommandError(f"capture file not found: {path}")
    return ReplayTransport(path)
