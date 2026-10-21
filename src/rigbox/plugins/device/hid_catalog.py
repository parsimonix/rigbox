"""Validate plugin-provided HID profiles and match them against HIDAPI enumeration.

Discovery annotates connected interfaces with known report layouts; named selection resolves one exact path
without opening a device or changing raw report bytes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

import hid
from pydantic import BaseModel, ConfigDict, Field, model_validator

from rigbox.core.registry import CommandError


class HIDReportSpec(BaseModel):
    """Describe one HID report and its optional application protocol binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: int = Field(..., ge=0, le=255, strict=True, description="Report ID; zero denotes an unnumbered report")
    kind: Literal["input", "output", "feature"] = Field(..., description="HID report transfer kind")
    length: int = Field(
        ...,
        ge=1,
        le=65536,
        strict=True,
        description="HIDAPI buffer size including any report ID, or the synthetic zero prefix for output/feature reports",
    )
    protocol: str | None = Field(None, pattern=r"^[a-z][a-z0-9_.-]*$", description="Descriptive protocol identifier; does not select a protocol")


class HIDDeviceSpec(BaseModel):
    """Describe a known HID interface and its input, output and feature reports."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vendor_id: int = Field(..., ge=0, le=65535, strict=True, description="Exact USB vendor ID")
    product_id: int = Field(..., ge=0, le=65535, strict=True, description="Exact USB product ID")
    description: str = Field("", description="Human-readable device description")
    usage_page: int | None = Field(None, ge=0, le=65535, strict=True, description="Optional exact HID usage page")
    usage: int | None = Field(None, ge=0, le=65535, strict=True, description="Optional exact HID usage")
    interface_number: int | None = Field(None, ge=0, le=255, strict=True, description="Optional exact USB interface number")
    reports: tuple[HIDReportSpec, ...] = Field(default_factory=tuple, description="Known report layouts and descriptive protocol bindings")

    @model_validator(mode="after")
    def unique_reports(self) -> HIDDeviceSpec:
        """Reject conflicting definitions for the same report ID and transfer kind.

        Returns:
            This validated profile.

        Raises:
            ValueError: If a report kind and ID occur more than once.
        """
        keys = [(report.kind, report.report_id) for report in self.reports]
        if len(keys) != len(set(keys)):
            raise ValueError("HID report kind and ID must be unique within a device profile")
        return self


class _HIDInterface(BaseModel):
    path: bytes = Field(min_length=1)
    vendor_id: int
    product_id: int
    serial_number: str | None = None
    manufacturer_string: str | None = None
    product_string: str | None = None
    usage_page: int | None = None
    usage: int | None = None
    interface_number: int | None = None

    def matches(self, spec: HIDDeviceSpec) -> bool:
        return (
            self.vendor_id == spec.vendor_id
            and self.product_id == spec.product_id
            and all(
                value is None or getattr(self, key) == value
                for key, value in (("usage_page", spec.usage_page), ("usage", spec.usage), ("interface_number", spec.interface_number))
            )
        )


def _enumerate_interfaces() -> list[_HIDInterface]:
    try:
        return [_HIDInterface.model_validate(info) for info in hid.enumerate()]
    except (OSError, ValueError) as error:
        raise CommandError(f"cannot enumerate HID devices: {error}") from error


def discover_hid(profiles: Mapping[str, HIDDeviceSpec], *, known_only: bool = False) -> list[dict[str, object]]:
    """Enumerate connected HID interfaces and annotate every matching plugin profile.

    Args:
        profiles: Registered device names and selectors.
        known_only: Omit interfaces with no matching profile.

    Returns:
        Serializable interface rows with matching profile names and declared reports.

    Raises:
        CommandError: If HIDAPI enumeration or its returned metadata is invalid.
    """
    rows: list[dict[str, object]] = []
    for interface in _enumerate_interfaces():
        matching = {name: spec for name, spec in sorted(profiles.items()) if interface.matches(spec)}
        if known_only and not matching:
            continue
        row: dict[str, object] = {
            "path": os.fsdecode(interface.path),
            "vendor_id": interface.vendor_id,
            "product_id": interface.product_id,
            "serial_number": interface.serial_number,
            "manufacturer": interface.manufacturer_string,
            "product": interface.product_string,
            "usage_page": interface.usage_page,
            "usage": interface.usage,
            "interface_number": interface.interface_number,
            "profiles": list(matching),
            "reports": {name: [report.model_dump(mode="json") for report in spec.reports] for name, spec in matching.items()},
        }
        rows.append(row)
    return rows


def select_hid_device(profiles: Mapping[str, HIDDeviceSpec], params: Mapping[str, str]) -> dict[str, str]:
    """Resolve a named profile to one exact interface path without opening it.

    Args:
        profiles: Registered device names and selectors.
        params: Transport parameters including device and optional serial_number or path filters.

    Returns:
        Transport parameters with the profile selectors replaced by the selected path.

    Raises:
        CommandError: If the profile is unknown, selectors conflict, enumeration fails or the match is not unique.
    """
    name = params["device"].strip().lower()
    spec = profiles.get(name)
    if spec is None:
        raise CommandError(f"unknown HID device profile {name!r} (known profiles: {', '.join(sorted(profiles)) or 'none'})")
    if "vendor_id" in params or "product_id" in params:
        raise CommandError("HID device profile cannot be combined with vendor_id or product_id")
    if any(key in params and not params[key] for key in ("serial_number", "path")):
        raise CommandError("HID serial_number and path filters must be nonempty")
    matches = [
        interface
        for interface in _enumerate_interfaces()
        if interface.matches(spec)
        and ("serial_number" not in params or interface.serial_number == params["serial_number"])
        and ("path" not in params or os.fsdecode(interface.path) == params["path"])
    ]
    if not matches:
        raise CommandError(f"no connected HID interface matches profile {name!r} and the supplied filters")
    if len(matches) > 1:
        candidates = "; ".join(f"path={os.fsdecode(interface.path)!r}, serial_number={interface.serial_number!r}" for interface in matches)
        raise CommandError(f"multiple HID interfaces match profile {name!r}; specify --param serial_number=... or --param path=...: {candidates}")
    result = {key: value for key, value in params.items() if key not in {"device", "serial_number", "path"}}
    result["path"] = os.fsdecode(matches[0].path)
    return result
