"""Project workspaces: a directory anchored by `rigbox.yaml`."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from rigbox.constants import AppFiles


class WorkspaceConfig(BaseModel):
    """Contents of ``rigbox.yaml``: workspace name and directory layout."""

    name: str = Field(..., description="The name of the workspace")
    version: int = Field(1, description="The version of the workspace")
    plugins_dir: str = Field("plugins", description="Directory for workspace-local plugin modules")
    workflows_dir: str = Field("workflows", description="Directory for saved workflows")
    manifests_dir: str = Field("manifests", description="Directory for data manifests")
    captures_dir: str = Field("captures", description="Directory for device captures")


class Workspace:
    """A workspace directory (the one containing ``rigbox.yaml``) and its configured subdirectories."""

    def __init__(self, root: Path, config: WorkspaceConfig) -> None:
        """Bind the workspace to its root directory and parsed config."""
        self.root = root
        self.config = config

    @property
    def plugins_dir(self) -> Path:
        """Directory of workspace-local plugin modules."""
        return self.root / self.config.plugins_dir

    @property
    def workflows_dir(self) -> Path:
        """Directory of saved workflows."""
        return self.root / self.config.workflows_dir

    @property
    def manifests_dir(self) -> Path:
        """Directory of data manifests."""
        return self.root / self.config.manifests_dir

    @property
    def captures_dir(self) -> Path:
        """Directory of device captures."""
        return self.root / self.config.captures_dir

    def find_file(self, directory: Path, ref: str) -> Path | None:
        """Look up `ref` in `directory`, with or without a .yaml/.yml suffix."""
        for suffix in ("", ".yaml", ".yml"):
            candidate = directory / f"{ref}{suffix}"
            if candidate.is_file():
                return candidate
        return None

    @property
    def data_dir(self) -> Path:
        """Directory of runtime state (history etc.); git-ignored."""
        return self.root / ".rigbox"

    @classmethod
    def load(cls, root: Path) -> Workspace:
        """Load the workspace rooted at `root` from its ``rigbox.yaml``."""
        data = yaml.safe_load((root / AppFiles.WORKSPACE_CONFIG).read_text(encoding="utf-8")) or {}
        return cls(root, WorkspaceConfig.model_validate(data))

    @classmethod
    def find(cls, start: Path) -> Workspace | None:
        """Find the workspace containing `start` by searching it and its parents, or return None."""
        start = start.resolve()
        for d in (start, *start.parents):
            if (d / AppFiles.WORKSPACE_CONFIG).is_file():
                return cls.load(d)
        return None

    @classmethod
    def init(cls, root: Path, name: str | None = None) -> Workspace:
        """Create a new workspace at `root` (named after the directory by default).

        Raises:
            FileExistsError: If `root` already contains a ``rigbox.yaml``.
        """
        root = root.resolve()
        if (root / AppFiles.WORKSPACE_CONFIG).exists():
            raise FileExistsError(f"{AppFiles.WORKSPACE_CONFIG} already exists in {root}")
        root.mkdir(parents=True, exist_ok=True)
        ws = cls(root, WorkspaceConfig(name=name or root.name))
        (root / AppFiles.WORKSPACE_CONFIG).write_text(yaml.safe_dump(ws.config.model_dump(), sort_keys=False), encoding="utf-8")
        for d in (ws.plugins_dir, ws.workflows_dir, ws.manifests_dir, ws.captures_dir):
            d.mkdir(exist_ok=True)
        (root / ".gitignore").write_text(".rigbox/\n", encoding="utf-8")
        return ws
