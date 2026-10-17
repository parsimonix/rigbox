"""Common constants for Rigbox."""

from __future__ import annotations

from typing import Final, Literal, TypeAlias


class AppMetadata:
    """Namespace class for application metadata-related constants."""

    NAME: Final[str] = "rigbox"
    VERSION: Final[str] = "0.1.0"  # Example version, replace with actual version if needed


class OutputFormats:
    """Namespace class for output format-related constants."""

    ANNOTATION: TypeAlias = Literal["table", "json", "ndjson", "auto"]
    VALUES: Final[tuple[str, ...]] = ("table", "json", "ndjson", "auto")
    DEFAULT_VALUE: Final[str] = "auto"


class Plugins:
    """Namespace class for plugin-related constants."""

    PROJECT: Final[str] = AppMetadata.NAME
    ENTRYPOINT_GROUP: Final[str] = f"{AppMetadata.NAME}.plugins"
    BUILTIN_MODULES: Final[tuple[str, ...]] = (
        f"{ENTRYPOINT_GROUP}.builtin",
        f"{ENTRYPOINT_GROUP}.manifests.commands",
        f"{ENTRYPOINT_GROUP}.device.commands",
        f"{ENTRYPOINT_GROUP}.statemachine.commands",
        f"{ENTRYPOINT_GROUP}.dsp.commands",
    )


class Files:
    """Namespace class for file-related constants."""

    SHELL_HISTORY: Final[str] = "shell_history.json"
