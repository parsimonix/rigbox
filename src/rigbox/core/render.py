"""Result contract and output renderers.

Handlers return None, a pydantic model / dict, or a list of them. `normalize` turns
that into `list[dict]` rows; `render` formats rows as table, json or ndjson.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from typing import Any, TextIO

from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

FORMATS = ("auto", "table", "json", "ndjson")


def normalize(result: object) -> list[dict[str, Any]]:
    """Turn a handler result (None, a model/dict, or a list of them) into a list of dict rows.

    Typed `object` rather than `CommandResult` because any other value is tolerated and wrapped as
    ``{"value": ...}``.
    """
    if result is None:
        return []
    items = result if isinstance(result, (list, tuple)) else [result]
    rows: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, BaseModel):
            rows.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            rows.append(item)
        else:
            rows.append({"value": item})
    return rows


def resolve_format(fmt: str, stream: TextIO | None = None) -> str:
    """Resolve ``auto`` to ``table`` on a TTY (default stdout) or ``ndjson``; pass other formats through."""
    if fmt != "auto":
        return fmt
    stream = stream or sys.stdout
    return "table" if getattr(stream, "isatty", lambda: False)() else "ndjson"


def _cell(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return "" if value is None else str(value)


def _table(rows: list[dict[str, Any]]) -> str:
    cols = list(dict.fromkeys(key for row in rows for key in row))
    table = Table(*cols, box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    for row in rows:
        table.add_row(*(Text(_cell(row.get(c))) for c in cols))  # Text: no markup interpretation
    buf = io.StringIO()
    width = shutil.get_terminal_size((120, 24)).columns
    Console(file=buf, width=width, color_system=None).print(table)
    return buf.getvalue().rstrip()


def render(rows: list[dict[str, Any]], fmt: str) -> str:
    """Format rows as ``table``, ``json`` or ``ndjson`` (``auto`` is resolved first).

    Raises:
        ValueError: If `fmt` is not a known format.
    """
    fmt = resolve_format(fmt)
    if not rows:
        return "[]" if fmt == "json" else ""
    if fmt == "json":
        return json.dumps(rows, indent=2)
    if fmt == "ndjson":
        return "\n".join(json.dumps(r) for r in rows)
    if fmt == "table":
        return _table(rows)
    raise ValueError(f"unknown format: {fmt}")
