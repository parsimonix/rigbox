"""Common constants for Rigbox."""

from __future__ import annotations

from enum import StrEnum
from importlib.metadata import version
from typing import Final, Self


class AppMetadata:
    """Namespace class for application metadata-related constants."""

    NAME: Final[str] = "rigbox"
    VERSION: Final[str] = version(NAME)


class AppFiles:
    """Namespace class for file-related constants."""

    SHELL_HISTORY: Final[str] = "shell_history.json"
    WORKSPACE_CONFIG: Final[str] = "rigbox.yaml"


class RenderFormat(StrEnum):
    """Enumeration of output rendering formats.

    This dictates how the output should be formatted when rendered, including options for JSON, NDJSON, Rich text, plain text,
    or automatic detection based on the terminal capabilities.
    """

    JSON = "json"
    NDJSON = "ndjson"
    RICH = "rich"
    PLAINTEXT = "plaintext"
    AUTO = "auto"

    @classmethod
    def default(cls) -> Self:
        """Get the default rendering format."""
        return cls.AUTO


class RenderLevel(StrEnum):
    """Enumeration of output rendering severity levels.

    This dictates how messages of different severity levels should be rendered, including their associated colors.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    SUCCESS = "success"

    @classmethod
    def default(cls) -> Self:
        """Get the default rendering level."""
        return cls.INFO

    @property
    def color(self) -> str:
        """Get the associated color for this `RenderLevel`.

        Raises:
            KeyError: If the `RenderLevel` does not have an associated color mapping.

        Returns:
            str: The rich color associated with this `RenderLevel`.
        """
        return {
            "error": "bold red",
            "warning": "yellow",
            "info": "default",  # No color
            "success": "green",
        }[self]


class Plugins:
    """Namespace class for plugin-related constants."""

    PROJECT: Final[str] = AppMetadata.NAME
    ENTRYPOINT_GROUP: Final[str] = f"{PROJECT}.plugins".lower()
    BUILTIN_MODULES: tuple[str, ...] = (
        f"{ENTRYPOINT_GROUP}.builtin",
        f"{ENTRYPOINT_GROUP}.manifests.commands",
        f"{ENTRYPOINT_GROUP}.device.commands",
        f"{ENTRYPOINT_GROUP}.statemachine.commands",
        f"{ENTRYPOINT_GROUP}.codegen.commands",
        f"{ENTRYPOINT_GROUP}.dsp.commands",
    )
