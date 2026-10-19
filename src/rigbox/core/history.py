"""Structured session history (JSONL). Steps replay and convert to workflows."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class History:
    """The commands run in a session, optionally persisted to a JSONL file."""

    def __init__(self, path: Path | None = None) -> None:
        """Load existing entries from `path` if it exists; with no path, keep history in memory only."""
        self.path = path
        self._entries: list[dict[str, Any]] = []
        if path is not None and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._entries.append(json.loads(line))

    def append(self, run: str, args: dict[str, Any], ok: bool = True) -> None:
        """Record a command run (name, args, success flag) and write it to the file, if any.

        Args:
            run (str): The name of the command that was run.
            args (dict[str, Any]): The arguments passed to the command.
            ok (bool, optional): Whether the command ran successfully. Defaults to True.
        """
        entry = {"ts": round(time.time(), 3), "run": run, "args": args, "ok": ok}
        self._entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        """Return a copy of all entries, oldest first.

        Returns:
            list[dict[str, Any]]: A list of all history entries, each being a dictionary with keys "ts", "run", "args", and "ok".
        """
        return list(self._entries)
