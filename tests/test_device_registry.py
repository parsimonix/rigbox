"""Verify plugin-owned device registration without depending on plugin load order."""

from __future__ import annotations

from pathlib import Path

import pytest

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import Registry
from rigbox.core.workspace import Workspace
from rigbox.plugins.device.commands import rigbox_register
from rigbox.plugins.device.protocols.line_protocol import open_line
from rigbox.plugins.device.registry import DeviceRegistry
from rigbox.plugins.device.transport import Transport, TransportOptions
from rigbox.plugins.device.transports.serial_transport import open_serial


def test_extensions_are_shared_per_type_and_isolated_per_registry() -> None:
    registry = Registry()
    devices = registry.extension(DeviceRegistry)
    assert registry.extension(DeviceRegistry) is devices
    assert registry.extension(DeviceRegistry) is not Registry().extension(DeviceRegistry)

    class OtherExtension:
        pass

    other = registry.extension(OtherExtension)
    assert registry.extension(OtherExtension) is other
    assert registry.extension(DeviceRegistry) is devices
    devices.add_protocol("custom", open_line)
    assert Registry().extension(DeviceRegistry).protocols == {}


@pytest.mark.parametrize("custom_first", [True, False])
def test_device_registration_works_in_either_hook_order(custom_first: bool) -> None:
    registry = Registry()

    def custom_hook() -> None:
        devices = registry.extension(DeviceRegistry)

        def open_loop(url: str, options: TransportOptions) -> Transport:
            return open_serial("loop://", options)

        devices.add_transport("custom", open_loop)
        devices.add_protocol("custom-line", open_line)

    if custom_first:
        custom_hook()
        rigbox_register(registry)
    else:
        rigbox_register(registry)
        custom_hook()

    context = Context(registry=registry)
    try:
        context.run("device.connect", {"name": "d", "url": "custom://", "timeout": 0.1})
        assert context.run(
            "device.request",
            {
                "name": "d",
                "protocol": "custom-line",
                "text": "hello",
            },
        ).structured_data == {"size": 5, "hex": "68656c6c6f"}
        assert context.run(
            "device.request",
            {
                "name": "d",
                "hex": "0102",
                "response_size": 2,
            },
        ).structured_data == {"size": 2, "hex": "0102"}
    finally:
        context.close()
    assert context.connections == {}


def test_workspace_plugin_registers_transport_and_protocol_through_device_api(
    tmp_path: Path,
) -> None:
    workspace = Workspace.init(tmp_path / "project")
    (workspace.plugins_dir / "comms.py").write_text(
        "from rigbox.hooks import hookimpl\n"
        "from rigbox.plugins.device.line_protocol import open_line\n"
        "from rigbox.plugins.device.registry import DeviceRegistry\n"
        "from rigbox.plugins.device.serial_transport import open_serial\n"
        "def open_loop(url, options):\n"
        "    return open_serial('loop://', options)\n"
        "@hookimpl\n"
        "def rigbox_register(registry):\n"
        "    devices = registry.extension(DeviceRegistry)\n"
        "    devices.add_transport('workspace', open_loop)\n"
        "    devices.add_protocol('workspace-line', open_line)\n",
        encoding="utf-8",
    )
    registry = Registry()
    load_all(registry, workspace)
    info = next(plugin for plugin in registry.plugins if plugin.name == "comms")
    assert info.error is None
    context = Context(registry=registry, workspace=workspace)
    try:
        context.run("device.connect", {"name": "d", "url": "workspace://", "timeout": 0.1})
        assert context.run(
            "device.request",
            {
                "name": "d",
                "protocol": "workspace-line",
                "text": "ok",
            },
        ).structured_data == {"size": 2, "hex": "6f6b"}
    finally:
        context.close()
