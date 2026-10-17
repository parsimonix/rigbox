"""Replay transport: play a `device.capture` file back as if a device were sending it."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from rigbox.core.registry import CommandError
from rigbox.core.transport import Transport, TransportOptions


class ReplayTransport:
    """Serves the received bytes recorded in a capture file; writes are kept in `written` and otherwise ignored."""

    def __init__(self, path: Path) -> None:
        """Load the received (``dir: rx``) records of the NDJSON capture at `path`.

        Raises:
            CommandError: If the file cannot be read or is not a capture.
        """
        chunks: list[bytes] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    if record.get("dir", "rx") == "rx" and isinstance(record.get("hex"), str):
                        chunks.append(bytes.fromhex(record["hex"]))
        except (OSError, ValueError, AttributeError) as e:
            raise CommandError(f"cannot read capture {path}: {e}") from None
        self._chunks = deque(chunks)
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        """Remember `data`; a recording cannot respond to it."""
        self.written.append(data)

    def read(self, size: int, timeout: float) -> bytes:
        """Return up to `size` of the remaining recorded bytes at once, ignoring `timeout`.

        Raises:
            EOFError: If the recording is exhausted.
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

    def read_chunk(self, timeout: float) -> bytes:
        """Return the next recorded packet as it was captured, ignoring `timeout`.

        Raises:
            EOFError: If the recording is exhausted.
        """
        if not self._chunks:
            raise EOFError("end of capture")
        return self._chunks.popleft()

    def close(self) -> None:
        """Discard what is left of the recording."""
        self._chunks.clear()


def open_replay(url: str, options: TransportOptions) -> Transport:
    """Play back a capture file: replay://path/to/capture.ndjson."""
    path = Path(url.partition("://")[2])
    if not path.is_file():
        raise CommandError(f"capture file not found: {path}")
    return ReplayTransport(path)
