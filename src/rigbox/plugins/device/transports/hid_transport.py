"""Adapt explicit HIDAPI reports to byte-stream reads without removing report IDs or padding.

Each write is one complete output report, including its report-ID byte (zero for unnumbered reports).
Reads retain exactly HIDAPI's input bytes: numbered reports include their ID, unnumbered reports do not.
Partial reads buffer the remainder of a report; separate reports are not combined by a single transport read.
"""

from __future__ import annotations

import math
import os
from typing import ClassVar

import hid

from rigbox.core.registry import CommandError
from rigbox.plugins.device.transport import Transport, TransportOptions


class HIDTransport:
    """Send explicit output reports and retain all input bytes through HIDAPI.

    HIDAPI does not expose a write timeout in this binding. Writes are neither padded, split nor retried;
    callers must supply report sizes and IDs appropriate for their device.
    """

    MAX_REPORT_SIZE: ClassVar[int] = 65536

    def __init__(self, url: str, options: TransportOptions) -> None:
        """Open a HID device by VID/PID and optional serial number, or by its enumerated path.

        Args:
            url: The transport URL, which must be ``hid://``.
            options: Parameters ``vendor_id`` and ``product_id`` accept decimal or 0x-prefixed hex;
                ``serial_number`` selects a particular device. Alternatively, supply ``path`` alone.
                ``read_size`` sets the maximum complete input report size, including any report ID,
                and defaults to 65536. It must not be smaller than the device's largest input report.

        Raises:
            CommandError: If options are invalid or the device cannot be opened.
        """
        if url.lower() != "hid://":
            raise CommandError("use hid:// with --param vendor_id=... --param product_id=... or --param path=...")
        if options.write_timeout_s is not None:
            raise CommandError("HIDAPI does not support --write-timeout")
        params = options.params
        unknown = params.keys() - {"vendor_id", "product_id", "serial_number", "path", "read_size"}
        if unknown:
            raise CommandError(f"unknown HID parameter(s): {', '.join(sorted(unknown))}")
        self.vendor_id: int | None = None
        self.product_id: int | None = None
        self._device: hid.device | None = None
        self._pending = b""
        self._read_size = _integer_param(params.get("read_size", str(self.MAX_REPORT_SIZE)), "read_size", 1, self.MAX_REPORT_SIZE)
        path = params.get("path")
        if path is not None:
            if not path or any(key in params for key in ("vendor_id", "product_id", "serial_number")):
                raise CommandError("HID path must be nonempty and cannot be combined with vendor_id, product_id or serial_number")
        else:
            if "vendor_id" not in params or "product_id" not in params:
                raise CommandError("HID requires vendor_id and product_id, or path")
            self.vendor_id = _integer_param(params["vendor_id"], "vendor_id", 1, 65535)
            self.product_id = _integer_param(params["product_id"], "product_id", 1, 65535)
        device = hid.device()
        try:
            if path is not None:
                device.open_path(os.fsencode(path))
            elif self.vendor_id is not None and self.product_id is not None:
                device.open(self.vendor_id, self.product_id, params.get("serial_number"))
        except (OSError, ValueError) as error:
            try:
                device.close()
            except OSError:
                pass
            raise CommandError(f"cannot open HID device: {error}") from error
        self._device = device

    def write(self, data: bytes) -> None:
        """Send one report, including its report ID, without padding, splitting or retrying.

        Args:
            data: Complete output report; use a zero first byte for an unnumbered report.

        Raises:
            OSError: If closed, the write fails, or HIDAPI accepts fewer bytes than supplied.
        """
        device = self._open_device()
        if not data:
            return
        written = device.write(data)
        if written != len(data):
            raise OSError(f"incomplete HID write: {written} of {len(data)} bytes")

    def read(self, size: int, timeout_s: float) -> bytes:
        """Return up to size bytes, retaining the remainder of a complete input report.

        Args:
            size: Maximum number of bytes to return.
            timeout_s: Maximum wait in seconds; zero polls without blocking.

        Returns:
            Input bytes, including any numbered report ID, or empty bytes on timeout.

        Raises:
            OSError: If closed or HIDAPI fails.
            ValueError: If size or timeout is invalid.
        """
        device = self._open_device()
        timeout_ms = _timeout_ms(timeout_s)
        if size < 0:
            raise ValueError("HID read size must be nonnegative")
        if size == 0:
            return b""
        if not self._pending:
            self._pending = bytes(device.read(self._read_size, timeout_ms))
        result, self._pending = self._pending[:size], self._pending[size:]
        return result

    def read_chunk(self, timeout_s: float) -> bytes:
        """Return a complete input report or the unread remainder of the previous report.

        Args:
            timeout_s: Maximum wait in seconds; zero polls without blocking.

        Returns:
            Unmodified HIDAPI input bytes, or empty bytes on timeout.

        Raises:
            OSError: If closed or HIDAPI fails.
            ValueError: If timeout is invalid.
        """
        return self.read(self._read_size, timeout_s)

    def close(self) -> None:
        """Close the HID device once and discard any unread bytes."""
        device, self._device = self._device, None
        self._pending = b""
        if device is not None:
            device.close()

    def _open_device(self) -> hid.device:
        if self._device is None:
            raise OSError("HID transport is closed")
        return self._device


def _integer_param(value: str, name: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value, 16 if value.lower().startswith("0x") else 10)
    except ValueError:
        raise CommandError(f"HID {name} must be a decimal or 0x-prefixed integer") from None
    if not minimum <= result <= maximum:
        raise CommandError(f"HID {name} must be between {minimum} and {maximum}")
    return result


def _timeout_ms(timeout_s: float) -> int:
    if not math.isfinite(timeout_s) or timeout_s < 0 or timeout_s > 2147483.647:
        raise ValueError("HID read timeout must be finite and between 0 and 2147483.647 seconds")
    return math.ceil(timeout_s * 1000)


def open_hid(url: str, options: TransportOptions) -> Transport:
    """Open explicit HID reports via hid:// with vendor_id/product_id or path parameters.

    Args:
        url: The ``hid://`` transport URL.
        options: HID device selectors and optional maximum input report size.

    Returns:
        The opened HID transport.

    Raises:
        CommandError: If options are invalid or the device cannot be opened.
    """
    return HIDTransport(url, options)
