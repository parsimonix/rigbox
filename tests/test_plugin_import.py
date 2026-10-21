"""Verify workspace modules follow Python's module registration and failure cleanup rules."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from rigbox.core.plugins import _import_file


def test_workspace_module_is_registered_during_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "rigbox_workspace_plugin_registered"
    monkeypatch.delitem(sys.modules, name, raising=False)
    path = tmp_path / "registered.py"
    path.write_text(
        "from __future__ import annotations\n"
        "import sys\n"
        "from dataclasses import dataclass\n"
        "registered = __name__ in sys.modules\n"
        "@dataclass\n"
        "class Example:\n"
        "    value: int\n",
        encoding="utf-8",
    )
    try:
        module = _import_file(path)
        assert module.registered is True
        assert sys.modules[name] is module
        assert module.Example(3).value == 3
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("existing", [False, True])
def test_failed_workspace_import_restores_module_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    name = "rigbox_workspace_plugin_broken"
    previous = ModuleType(name)
    if existing:
        monkeypatch.setitem(sys.modules, name, previous)
    else:
        monkeypatch.delitem(sys.modules, name, raising=False)
    path = tmp_path / "broken.py"
    path.write_text("raise RuntimeError('import failed')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="import failed"):
        _import_file(path)
    if existing:
        assert sys.modules[name] is previous
    else:
        assert name not in sys.modules
