"""device.* commands: ports, transports, connect, close, list, send, expect, capture.

Connections are named and live on the Context, so they last for a REPL session or one workflow run. A URL's scheme
picks the transport (see `device.transports`): `loop://` is a loopback port that echoes what you write, and
`replay://<capture>` plays a recorded capture back, so you can try things without hardware.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from serial.tools import list_ports

from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, NoArgs, Registry
from rigbox.core.transport import TransportOptions
from rigbox.hooks import hookimpl
from rigbox.plugins.device.connection import Connection
from rigbox.plugins.device.replay_transport import open_replay
from rigbox.plugins.device.serial_transport import open_serial
from rigbox.plugins.manifests.api import Codec, parse_values, resolve_manifest

if TYPE_CHECKING:
    from rigbox.core.context import Context


class ConnectArgs(BaseModel):
    """Arguments for `device.connect`."""

    name: str = Field(description="Name to refer to this connection by")
    url: str = Field(description="A serial port (COM3), or a URL whose scheme picks the transport (device.transports)")
    baud: int = Field(115200, ge=1, description="Serial baud rate (ignored by transports without one)")
    timeout: float = Field(1.0, gt=0, description="Default read timeout in seconds")
    param: list[str] = Field(default_factory=list, description="Transport-specific option key=value (repeatable)")


class NameArgs(BaseModel):
    """Arguments for commands that act on one named connection."""

    name: str = Field(description="Connection name")


class SendArgs(NameArgs):
    """Arguments for `device.send`."""

    hex: str | None = Field(None, description="Raw bytes as hex")
    manifest: str | None = path_field(None, description="Pack a packet with this manifest (needs --values)")
    values: str | None = Field(None, description="Field values, YAML/JSON flow mapping")


class ExpectArgs(NameArgs):
    """Arguments for `device.expect`."""

    manifest: str | None = path_field(None, description="Read one packet and decode it with this manifest")
    size: int | None = Field(None, ge=1, description="Read exactly this many raw bytes")
    timeout: float | None = Field(None, gt=0, description="Seconds to wait (default: the connection's timeout)")


class CaptureArgs(NameArgs):
    """Arguments for `device.capture`."""

    seconds: float = Field(10.0, gt=0, description="Stop after this long")
    count: int | None = Field(None, ge=1, description="Stop after this many packets")
    manifest: str | None = path_field(None, description="Split the stream into packets and decode with this manifest")
    size: int | None = Field(None, ge=1, description="Split the stream into fixed-size raw packets")
    out: str | None = path_field(None, description="Output NDJSON file (default: workspace captures dir)")


def _get(ctx: Context, name: str) -> Connection:
    conn = ctx.connections.get(name)
    if not isinstance(conn, Connection):  # also rejects another plugin's resource that happens to share the name
        raise CommandError(f"no connection named '{name}' (use device.connect)")
    return conn


def _parse_params(items: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in items:
        key, equals, value = item.partition("=")
        if not equals or not key:
            raise CommandError(f"--param expects key=value, got: {item}")
        params[key] = value
    return params


def _capture_path(ctx: Context, name: str, out: str | None) -> Path:
    if out:
        return Path(out)
    base = ctx.workspace.captures_dir if ctx.workspace else Path.cwd()
    return base / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ndjson"


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `device.*` commands and the built-in transports."""
    for scheme in ("serial", "loop", "socket", "rfc2217"):
        registry.add_transport(scheme, open_serial)
    registry.add_transport("replay", open_replay)

    @registry.command("device.transports", record=False)
    def transports(ctx: Context, args: NoArgs) -> list[dict[str, str]]:
        """List the transports (URL schemes) that device.connect understands."""
        return [
            {"scheme": scheme, "description": ((factory.__doc__ or "").strip().splitlines() or [""])[0]}
            for scheme, factory in sorted(ctx.registry.transports.items())
        ]

    @registry.command("device.ports", record=False)
    def ports(ctx: Context, args: NoArgs) -> list[dict[str, str]]:
        """List serial ports on this machine."""
        return [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in list_ports.comports()]

    @registry.command("device.connect", args=ConnectArgs)
    def connect(ctx: Context, args: ConnectArgs) -> dict[str, Any]:
        """Open a named connection to a device."""
        if args.name in ctx.connections:
            raise CommandError(f"connection '{args.name}' already exists")
        options = TransportOptions(timeout=args.timeout, baud=args.baud, params=_parse_params(args.param))
        transport = ctx.registry.open_transport(args.url, options)
        connection = Connection(args.name, args.url, transport, options)
        ctx.connections[args.name] = connection
        return {"name": args.name, "url": args.url, "transport": connection.scheme}

    @registry.command("device.close", args=NameArgs)
    def close(ctx: Context, args: NameArgs) -> None:
        """Close a named connection."""
        _get(ctx, args.name).close()
        del ctx.connections[args.name]

    @registry.command("device.list", record=False)
    def list_(ctx: Context, args: NoArgs) -> list[dict[str, Any]]:
        """List open connections."""
        return [
            {"name": c.name, "url": c.url, "transport": c.scheme, "timeout": c.timeout}
            for c in ctx.connections.values()
            if isinstance(c, Connection)
        ]

    @registry.command("device.send", args=SendArgs)
    def send(ctx: Context, args: SendArgs) -> dict[str, Any]:
        """Send raw hex bytes, or a packet packed from a manifest."""
        conn = _get(ctx, args.name)
        if args.hex is not None and args.manifest is None:
            try:
                data = bytes.fromhex(args.hex.replace(" ", ""))
            except ValueError as e:
                raise CommandError(f"bad hex: {e}") from None
        elif args.manifest is not None and args.values is not None and args.hex is None:
            data = Codec(resolve_manifest(ctx, args.manifest)).encode(parse_values(args.values))
        else:
            raise CommandError("give either --hex, or --manifest together with --values")
        conn.write(data)
        return {"name": args.name, "sent": len(data), "hex": data.hex()}

    @registry.command("device.expect", args=ExpectArgs)
    def expect(ctx: Context, args: ExpectArgs) -> dict[str, Any]:
        """Wait for one packet (manifest-sized or fixed size) and show it."""
        conn = _get(ctx, args.name)
        if (args.manifest is None) == (args.size is None):
            raise CommandError("give exactly one of --manifest or --size")
        codec = Codec(resolve_manifest(ctx, args.manifest)) if args.manifest else None
        size = codec.size if codec else args.size
        assert size is not None  # exactly one of manifest/size was given (checked above)
        ended = False
        try:
            data = conn.read(size, args.timeout or conn.timeout)
        except EOFError:
            data, ended = b"", True
        if len(data) < size:
            reason = "stream ended" if ended else "timeout"
            raise CommandError(f"{reason}: got {len(data)} of {size} bytes ({data.hex()})")
        return codec.decode(data) if codec else {"size": len(data), "hex": data.hex()}

    @registry.command("device.capture", args=CaptureArgs)
    def capture(ctx: Context, args: CaptureArgs) -> dict[str, Any]:
        """Record incoming packets as timestamped NDJSON (stops at --seconds or --count)."""
        conn = _get(ctx, args.name)
        if args.manifest and args.size:
            raise CommandError("give at most one of --manifest or --size")
        codec = Codec(resolve_manifest(ctx, args.manifest)) if args.manifest else None
        size = codec.size if codec else args.size  # None = raw chunks as they arrive
        path = _capture_path(ctx, args.name, args.out)
        path.parent.mkdir(parents=True, exist_ok=True)

        end = time.monotonic() + args.seconds
        packets = errors = 0
        with path.open("w", encoding="utf-8") as f:
            while (args.count is None or packets < args.count) and time.monotonic() < end:
                wait = min(conn.timeout, end - time.monotonic())
                try:
                    data = conn.read(size, wait) if size else conn.read_chunk(wait)
                except EOFError:  # a finished recording: nothing more will ever arrive
                    break
                if not data:
                    continue
                record: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "dir": "rx", "hex": data.hex()}
                if size and len(data) < size:
                    record["error"] = f"partial packet ({len(data)} of {size} bytes)"
                elif codec:
                    try:
                        record["decoded"] = codec.decode(data)
                    except CommandError as e:
                        record["error"] = str(e)
                errors += "error" in record
                packets += 1
                f.write(json.dumps(record) + "\n")
        return {"file": str(path), "packets": packets, "errors": errors}
