"""Command responses and rendering harness for said responses.

Command handlers may return a string, structured data, or rich renderable content.
Dispatch will normalize all of these types of responses into a RigboxCmdResult object.
This object has a dedicated renderer (`RigboxCmdResult.render`) to display them appropriately depending on the active format setting.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Self, TextIO
from warnings import warn

from pydantic import BaseModel
from rich.console import Console, ConsoleRenderable, RenderableType, RichCast
from rich.table import Table
from rich.text import Text

from rigbox.constants import RenderFormat, RenderLevel


@dataclass(frozen=True)
class RigboxCmdResult:
    """Base class for Rigbox command results."""

    text: str | None = None
    structured_data: object | None = None
    rich_data: RenderableType | None = None
    fmt: RenderFormat = RenderFormat.default()
    level: RenderLevel = RenderLevel.default()

    @classmethod
    def empty(cls) -> Self:
        """Return an empty RigboxCmdResult."""
        return cls()

    @classmethod
    def normalized(cls, raw_cmd_resp: object) -> RigboxCmdResult:
        """Normalize a raw Rigbox command output to a formal RigboxCmdResult object.

        Args:
            raw_cmd_resp (object): The raw output from a Rigbox command, which can be a string, structured data, or rich renderable.

        Raises:
            ValueError: If the raw command response cannot be normalized into a RigboxCmdResult.

        Returns:
            A normalized command result.
        """
        match raw_cmd_resp:
            case RigboxCmdResult():
                return raw_cmd_resp
            case None:  # Empty response
                return RigboxCmdResult.empty()
            case str():  # Human-readable text
                return RigboxCmdResult(text=raw_cmd_resp)
            case BaseModel() | dict() | list() | tuple():  # Structured data
                return RigboxCmdResult(structured_data=raw_cmd_resp)
            case ConsoleRenderable() | RichCast():  # Rich renderable content
                return RigboxCmdResult(rich_data=raw_cmd_resp)
            case _:
                raise ValueError(f"Cannot normalize response of type {type(raw_cmd_resp)}")

    def render(self, fmt: RenderFormat, stream: TextIO | None = None) -> str:
        """Render this RigboxCmdResult to an output-ready str.

        Args:
            fmt (RenderFormat): The desired output format for rendering.
            stream (TextIO | None, optional): The output stream to which the rendered content will be written. Defaults to None, which uses sys.stdout.

        Raises:
            RuntimeError: If the resolved format is unexpected or unsupported.

        Returns:
            str: The rendered output as a string.
        """
        stream = stream if stream is not None else sys.stdout
        fmt = resolve_render_format(fmt, stream=stream)
        match fmt:
            case RenderFormat.PLAINTEXT:
                if self.text is None:
                    warn("No plaintext available for rendering.", stacklevel=1)
                    return ""
                return normalize_text(self.text, level=self.level, stream=stream)
            case RenderFormat.RICH:
                if self.rich_data is None:
                    warn("No rich data available for rendering.", stacklevel=1)
                    return ""
                return normalize_rich_renderable(self.rich_data, stream=stream)
            case RenderFormat.JSON:
                rows = normalize_structured_data(self.structured_data)
                if not rows:
                    warn("No structured data available for rendering as JSON.", stacklevel=1)
                    return "[]"
                return json.dumps(rows, indent=2)
            case RenderFormat.NDJSON:
                rows = normalize_structured_data(self.structured_data)
                if not rows:
                    warn("No structured data available for rendering as NDJSON.", stacklevel=1)
                    return ""
                return "\n".join(json.dumps(r) for r in rows)
            case _:
                raise RuntimeError(f"unexpected format after resolution: {fmt}")


def normalize_structured_data(result: object) -> list[dict[str, Any]]:
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


def normalize_rich_renderable(rich_renderable: RenderableType, stream: TextIO | None = None) -> str:
    """Style a Rich renderable object and render it to a string that can be printed to stdout.

    Args:
        rich_renderable (RenderableType): The Rich renderable object to be styled and rendered.
        stream (TextIO | None, optional): The output stream used to detect terminal color support. Defaults to None.

    Returns:
        str: The rendered string representation of the Rich renderable object.
    """
    stream = stream if stream is not None else sys.stdout
    color_enabled = is_color_enabled(stream)
    buffer = io.StringIO()
    Console(
        file=buffer,
        width=shutil.get_terminal_size((120, 24)).columns,
        color_system="auto" if color_enabled else None,
        force_terminal=color_enabled,
    ).print(rich_renderable)
    return buffer.getvalue()


def normalize_text(raw_text: str, level: RenderLevel = RenderLevel.default(), stream: TextIO | None = None) -> str:
    """Style a human-readable message for its destination terminal.

    Args:
        raw_text: The message and its severity or raw-text designation.
        level: The severity level of the message (e.g., "error", "warning", "info", "success").
        stream: Destination used to detect terminal color support; defaults to stdout.

    Returns:
        Message text with severity styling when terminal colors are enabled.
    """
    text = raw_text.strip()
    if not text:
        return text
    stream = stream if stream is not None else sys.stdout
    color_enabled = is_color_enabled(stream)
    buffer = io.StringIO()
    Console(
        file=buffer,
        width=shutil.get_terminal_size((120, 24)).columns,
        color_system="auto" if color_enabled else None,
        force_terminal=color_enabled,
    ).print(Text(text, style=level.color), end="", soft_wrap=True)
    return buffer.getvalue()


def is_color_enabled(stream: TextIO) -> bool:
    """Check if color is enabled for the given output stream.

    Args:
        stream (TextIO | None, optional): The output stream to check for color support. Defaults to None.

    Returns:
        bool: True if color is enabled for the given stream, False otherwise.
    """
    return getattr(stream, "isatty", lambda: False)() and not os.environ.get("NO_COLOR")


def resolve_render_format(fmt: RenderFormat, stream: TextIO | None = None) -> RenderFormat:
    """Resolve the given `RenderFormat` for the given stream, or stdout if none is given.

    Args:
        fmt (RenderFormat): The rendering format to resolve for the given stream.
        stream (TextIO | None, optional): The output stream to check for terminal capabilities. Defaults to None.

    Raises:
        ValueError: If the provided format is not recognized.

    Returns:
        The resolved rendering format for the given stream.
    """
    if fmt not in RenderFormat:
        raise ValueError(f"unknown format: {fmt}")
    if fmt != "auto":
        return fmt
    stream = stream or sys.stdout
    return RenderFormat.RICH if getattr(stream, "isatty", lambda: False)() else RenderFormat.NDJSON


def create_rich_table(
    title: str,
    columns: list[str],
    rows: list[list[str]],
    col_metadata: dict[str, dict[str, Any]] | None = None,
    show_lines: bool = False,
) -> Table:
    """Generate a Rich `Table` with the given information.

    Args:
        title (str): The title of the table.
        columns (list[str]): The list of column names.
        rows (list[list[str]]): The list of rows, each row being a list of cell values.
        col_metadata (dict[str, dict[str, Any]] | None, optional): Metadata for each column, such as style or justification. Defaults to None.
        show_lines (bool, optional): Whether to show lines between rows. Defaults to False.

    Returns:
        Table: The generated Rich `Table` object.
    """
    table = Table(title=title, show_lines=show_lines)
    for col in columns:
        col_meta = col_metadata.get(col, {}) if col_metadata else {}
        table.add_column(col, **col_meta)
    for row in rows:
        table.add_row(*row)
    return table
