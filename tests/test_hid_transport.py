"""Exercise HID report adaptation without requiring physical hardware."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.core.workspace import Workspace
from rigbox.plugins.device import hid_catalog
from rigbox.plugins.device.hid_catalog import HIDDeviceSpec, HIDReportSpec, discover_hid, select_hid_device
from rigbox.plugins.device.registry import DeviceRegistry
from rigbox.plugins.device.transport import TransportOptions
from rigbox.plugins.device.transports import hid_transport
from rigbox.plugins.device.transports.hid_transport import HIDTransport


class FakeHIDDevice:
    """Record HIDAPI calls and supply complete input reports."""

    def __init__(self) -> None:
        """Initialize the fake device's reports and recorded calls."""
        self.opened: tuple[int, int, str | None] | bytes | None = None
        self.reports: list[list[int]] = []
        self.reads: list[tuple[int, int]] = []
        self.writes: list[bytes] = []
        self.write_result: int | None = None
        self.open_error: OSError | None = None
        self.io_error: OSError | None = None
        self.close_count = 0

    def open(self, vendor_id: int, product_id: int, serial_number: str | None = None) -> None:
        """Record device selectors or simulate an open failure.

        Args:
            vendor_id: USB vendor ID.
            product_id: USB product ID.
            serial_number: Optional serial number.
        """
        if self.open_error is not None:
            raise self.open_error
        self.opened = (vendor_id, product_id, serial_number)

    def open_path(self, path: bytes) -> None:
        """Record an enumerated HID path.

        Args:
            path: Device path bytes.
        """
        self.opened = path

    def read(self, max_length: int, timeout_ms: int) -> list[int]:
        """Return the next input report, truncated like HIDAPI.

        Args:
            max_length: Input buffer capacity.
            timeout_ms: Read timeout in milliseconds.

        Returns:
            The next report, or an empty list on timeout.
        """
        if self.io_error is not None:
            raise self.io_error
        self.reads.append((max_length, timeout_ms))
        return self.reports.pop(0)[:max_length] if self.reports else []

    def write(self, data: bytes) -> int:
        """Record an output report and return its accepted size.

        Args:
            data: The complete report.

        Returns:
            The configured write result, or the full report size.
        """
        if self.io_error is not None:
            raise self.io_error
        self.writes.append(data)
        return len(data) if self.write_result is None else self.write_result

    def close(self) -> None:
        """Record a close attempt."""
        self.close_count += 1


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> FakeHIDDevice:
    """Replace the native HIDAPI device factory with a recording fake.

    Args:
        monkeypatch: Test-scoped attribute replacement helper.

    Returns:
        The fake device returned by the patched factory.
    """
    fake = FakeHIDDevice()
    monkeypatch.setattr(hid_transport.hid, "device", lambda: fake)
    return fake


def make_transport(**params: str) -> HIDTransport:
    return HIDTransport("hid://", TransportOptions(params={"vendor_id": "0x1234", "product_id": "5678", **params}))


def test_open_selectors_and_path(device: FakeHIDDevice) -> None:
    transport = make_transport(serial_number="unit-1")
    assert device.opened == (0x1234, 5678, "unit-1")
    transport.close()
    transport = HIDTransport("hid://", TransportOptions(params={"path": r"\\?\hid#device"}))
    assert device.opened == b"\\\\?\\hid#device"
    transport.close()


def test_partial_reads_preserve_ids_padding_and_report_boundaries(device: FakeHIDDevice) -> None:
    device.reports = [[7, 10, 20, 0, 0], [30, 40]]
    transport = make_transport(read_size="64")
    assert transport.read(2, 0.0001) == b"\x07\x0a"
    assert transport.read_chunk(0) == b"\x14\x00\x00"
    assert device.reads == [(64, 1)]
    assert transport.read(10, 0) == b"\x1e\x28"
    assert device.reads[-1] == (64, 0)
    assert transport.read_chunk(0.25) == b""
    assert device.reads[-1] == (64, 250)


def test_write_sends_exactly_one_report_without_retry(device: FakeHIDDevice) -> None:
    transport = make_transport()
    transport.write(b"")
    transport.write(b"\x00\x01\x00")
    transport.write(b"\x07\x02")
    assert device.writes == [b"\x00\x01\x00", b"\x07\x02"]
    device.write_result = 1
    with pytest.raises(OSError, match="incomplete HID write: 1 of 3"):
        transport.write(b"\x00\x01\x02")
    assert len(device.writes) == 3


def test_close_is_idempotent_and_operations_fail_after_close(device: FakeHIDDevice) -> None:
    transport = make_transport()
    device.reports = [[1, 2]]
    assert transport.read(1, 0) == b"\x01"
    transport.close()
    transport.close()
    assert device.close_count == 1
    with pytest.raises(OSError, match="closed"):
        transport.read_chunk(0)
    with pytest.raises(OSError, match="closed"):
        transport.write(b"\x00")


def test_open_failure_closes_device_and_is_user_facing(device: FakeHIDDevice) -> None:
    device.open_error = OSError("access denied")
    with pytest.raises(CommandError, match="cannot open HID device: access denied"):
        make_transport()
    assert device.close_count == 1


def test_disconnect_errors_propagate(device: FakeHIDDevice) -> None:
    transport = make_transport()
    device.io_error = OSError("disconnected")
    with pytest.raises(OSError, match="disconnected"):
        transport.read_chunk(0)
    with pytest.raises(OSError, match="disconnected"):
        transport.write(b"\x00")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"vendor_id": "1"},
        {"vendor_id": "0", "product_id": "1"},
        {"vendor_id": "0x10000", "product_id": "1"},
        {"vendor_id": "nope", "product_id": "1"},
        {"path": ""},
        {"path": "device", "vendor_id": "1"},
        {"path": "device", "serial_number": "unit-1"},
        {"path": "device", "read_size": "0"},
        {"path": "device", "read_size": "65537"},
        {"path": "device", "unknown": "1"},
    ],
)
def test_invalid_options_do_not_open_device(device: FakeHIDDevice, params: dict[str, str]) -> None:
    with pytest.raises(CommandError):
        HIDTransport("hid://", TransportOptions(params=params))
    assert device.opened is None


def test_unsupported_url_and_write_timeout(device: FakeHIDDevice) -> None:
    with pytest.raises(CommandError, match="use hid://"):
        HIDTransport("hid://1234:5678", TransportOptions())
    with pytest.raises(CommandError, match="does not support --write-timeout"):
        HIDTransport("hid://", TransportOptions(write_timeout_s=1))
    assert device.opened is None


@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("inf"), 2147484.0])
def test_invalid_timeout(device: FakeHIDDevice, timeout: float) -> None:
    transport = make_transport()
    with pytest.raises(ValueError, match="timeout"):
        transport.read_chunk(timeout)
    assert not device.reads


def test_zero_size_does_not_consume_report(device: FakeHIDDevice) -> None:
    transport = make_transport()
    device.reports = [[1, 2]]
    assert transport.read(0, 0) == b""
    assert not device.reads
    with pytest.raises(ValueError, match="size"):
        transport.read(-1, 0)
    assert transport.read_chunk(0) == b"\x01\x02"


def test_device_commands_use_hid_reports(device: FakeHIDDevice) -> None:
    registry = Registry()
    load_all(registry)
    ctx = Context(registry=registry)
    try:
        ctx.run_line("device.connect usb hid:// --param vendor_id=0x1234 --param product_id=5678 --param read_size=64")
        ctx.run_line('device.send usb --hex "00 01 02"')
        assert device.writes == [b"\x00\x01\x02"]
        device.reports = [[7, 1, 2, 0]]
        assert ctx.run("device.expect", {"name": "usb", "size": 2}).structured_data == {"size": 2, "hex": "0701"}
        assert ctx.run("device.expect", {"name": "usb", "size": 2}).structured_data == {"size": 2, "hex": "0200"}
        ctx.run("device.close", {"name": "usb"})
        assert device.close_count == 1
    finally:
        ctx.close()


def test_hid_profiles_are_immutable_and_registry_local() -> None:
    report = HIDReportSpec(report_id=7, kind="input", length=64, protocol="line")
    spec = HIDDeviceSpec(vendor_id=0x1234, product_id=0x5678, reports=(report,))
    registry = DeviceRegistry()
    registry.add_hid_device("ACME.board", spec)
    assert registry.hid_devices == {"acme.board": spec}
    assert DeviceRegistry().hid_devices == {}
    with pytest.raises(ValueError, match="duplicate"):
        registry.add_hid_device("acme.board", spec)
    with pytest.raises(ValueError, match="invalid"):
        registry.add_hid_device("bad name", spec)
    with pytest.raises(ValidationError, match="frozen"):
        report.__setattr__("length", 1)
    with pytest.raises(ValidationError, match="unique"):
        HIDDeviceSpec(vendor_id=1, product_id=2, reports=(report, report))


@pytest.mark.parametrize("vendor_id", [-1, 65536, True, "0x1234"])
def test_hid_profiles_reject_invalid_ids(vendor_id: object) -> None:
    with pytest.raises(ValidationError):
        HIDDeviceSpec.model_validate({"vendor_id": vendor_id, "product_id": 2})


@pytest.mark.parametrize("report", [{"report_id": 256}, {"length": 0}, {"length": 65537}, {"kind": "invalid"}])
def test_hid_profiles_validate_report_sizes_and_kinds(report: dict[str, int | str]) -> None:
    with pytest.raises(ValidationError):
        HIDReportSpec.model_validate({"report_id": 1, "kind": "input", "length": 64, **report})


@pytest.fixture
def interfaces(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Supply known, composite and unknown HID interfaces without hardware.

    Args:
        monkeypatch: Test-scoped replacement helper.

    Returns:
        Mutable HIDAPI enumeration rows.
    """
    rows: list[dict[str, object]] = [
        {"path": b"board-a", "vendor_id": 0x1234, "product_id": 0x5678, "serial_number": "A", "usage_page": 0xFF00, "usage": 1, "interface_number": 0},
        {"path": b"board-b", "vendor_id": 0x1234, "product_id": 0x5678, "serial_number": "B", "usage_page": 0xFF00, "usage": 1, "interface_number": 0},
        {"path": b"keyboard", "vendor_id": 0x1234, "product_id": 0x5678, "serial_number": "A", "usage_page": 1, "usage": 6, "interface_number": 1},
        {"path": b"unknown", "vendor_id": 1, "product_id": 2},
    ]
    monkeypatch.setattr(hid_catalog.hid, "enumerate", lambda: rows)
    return rows


def board_profile() -> HIDDeviceSpec:
    return HIDDeviceSpec(
        vendor_id=0x1234,
        product_id=0x5678,
        description="Development board",
        usage_page=0xFF00,
        usage=1,
        interface_number=0,
        reports=(
            HIDReportSpec(report_id=7, kind="input", length=64, protocol="line"),
            HIDReportSpec(report_id=8, kind="output", length=32, protocol="line"),
            HIDReportSpec(report_id=9, kind="feature", length=16),
        ),
    )


def test_discovery_matches_all_profiles_and_keeps_unknown_devices(interfaces: list[dict[str, object]]) -> None:
    profiles = {"acme.board": board_profile(), "acme.alias": board_profile()}
    rows = discover_hid(profiles)
    assert len(rows) == len(interfaces)
    assert rows[0]["profiles"] == ["acme.alias", "acme.board"]
    assert rows[0]["reports"] == {name: [report.model_dump(mode="json") for report in spec.reports] for name, spec in profiles.items()}
    assert rows[2]["profiles"] == []
    assert rows[3]["profiles"] == []
    assert [row["path"] for row in discover_hid(profiles, known_only=True)] == ["board-a", "board-b"]
    assert len(discover_hid({})) == 4
    assert discover_hid({}, known_only=True) == []


def test_named_selection_requires_unique_matching_interface(interfaces: list[dict[str, object]]) -> None:
    profiles = {"acme.board": board_profile()}
    with pytest.raises(CommandError, match="multiple HID interfaces.*board-a.*board-b"):
        select_hid_device(profiles, {"device": "acme.board"})
    assert select_hid_device(profiles, {"device": "ACME.board", "serial_number": "A", "read_size": "128"}) == {"path": "board-a", "read_size": "128"}
    assert select_hid_device(profiles, {"device": "acme.board", "path": "board-b"}) == {"path": "board-b"}
    with pytest.raises(CommandError, match="no connected HID"):
        select_hid_device(profiles, {"device": "acme.board", "path": "keyboard"})
    with pytest.raises(CommandError, match="no connected HID"):
        select_hid_device(profiles, {"device": "acme.board", "serial_number": "missing"})
    with pytest.raises(CommandError, match="unknown HID device profile"):
        select_hid_device(profiles, {"device": "missing"})
    with pytest.raises(CommandError, match="cannot be combined"):
        select_hid_device(profiles, {"device": "acme.board", "vendor_id": "1"})


def test_discovery_and_named_connect_commands(device: FakeHIDDevice, interfaces: list[dict[str, object]]) -> None:
    registry = Registry()
    devices = registry.extension(DeviceRegistry)
    devices.add_hid_device("acme.board", board_profile())
    load_all(registry)
    ctx = Context(registry=registry)
    try:
        assert ctx.run_line("device.discover --known-only").structured_data == discover_hid(devices.hid_devices, known_only=True)
        with pytest.raises(CommandError, match="multiple HID interfaces"):
            ctx.run_line("device.connect usb hid:// --param device=acme.board")
        assert not ctx.connections
        assert device.opened is None
        ctx.run_line("device.connect usb hid:// --param device=acme.board --param serial_number=A")
        assert device.opened == b"board-a"
        ctx.run_line('device.send usb --hex "08 01 00"')
        assert device.writes == [b"\x08\x01\x00"]
    finally:
        ctx.close()
    assert device.close_count == 1


def test_enumeration_errors_are_user_facing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> list[dict[str, object]]:
        raise OSError("enumeration failed")

    monkeypatch.setattr(hid_catalog.hid, "enumerate", fail)
    with pytest.raises(CommandError, match="cannot enumerate HID devices: enumeration failed"):
        discover_hid({})
    with pytest.raises(CommandError, match="cannot enumerate HID devices: enumeration failed"):
        select_hid_device({"acme.board": board_profile()}, {"device": "acme.board"})


def test_workspace_plugin_can_register_hid_profiles(tmp_path: Path, interfaces: list[dict[str, object]], device: FakeHIDDevice) -> None:
    workspace = Workspace.init(tmp_path / "workspace")
    (workspace.plugins_dir / "known_devices.py").write_text(
        """from rigbox.core.registry import Registry
from rigbox.hooks import hookimpl
from rigbox.plugins.device.hid_catalog import HIDDeviceSpec, HIDReportSpec
from rigbox.plugins.device.registry import DeviceRegistry


@hookimpl
def rigbox_register(registry: Registry) -> None:
    registry.extension(DeviceRegistry).add_hid_device(
        "acme.board",
        HIDDeviceSpec(
            vendor_id=0x1234, product_id=0x5678, usage_page=0xFF00,
            reports=(HIDReportSpec(report_id=7, kind="input", length=64),),
        ),
    )
""",
        encoding="utf-8",
    )
    registry = Registry()
    load_all(registry, workspace)
    assert "acme.board" in registry.extension(DeviceRegistry).hid_devices
    ctx = Context(registry=registry, workspace=workspace)
    try:
        rows = ctx.run("device.discover", {"known_only": True}).structured_data
        assert isinstance(rows, list) and len(rows) == 2
        ctx.run_line("device.connect usb hid:// --param device=acme.board --param path=board-b")
        assert device.opened == b"board-b"
    finally:
        ctx.close()


def test_report_ids_can_repeat_across_transfer_kinds() -> None:
    reports = tuple(HIDReportSpec(report_id=7, kind=kind, length=64) for kind in ("input", "output", "feature"))
    spec = HIDDeviceSpec(vendor_id=1, product_id=2, reports=reports)
    assert spec.reports == reports
