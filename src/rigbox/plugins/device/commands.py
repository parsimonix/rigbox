"""Manage device connections and raw or protocol-framed send, expect, request and capture operations.

Connections are named and live on the Context, so they last for a REPL session or one workflow run. A URL's scheme
picks the transport (see `device.transports`): `loop://` is a loopback port that echoes what you write, and
`replay://<capture>` plays a recorded capture back, so you can try things without hardware. Each messaging command
selects --protocol independently; omitting it preserves the existing raw behavior.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator
from serial.tools import list_ports

from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, NoArgs, Registry
from rigbox.core.render import RigboxCmdResult
from rigbox.hooks import hookimpl
from rigbox.plugins.device.connection import Connection
from rigbox.plugins.device.hid_catalog import discover_hid, select_hid_device
from rigbox.plugins.device.protocol import Message
from rigbox.plugins.device.protocols.line_protocol import open_line
from rigbox.plugins.device.registry import DeviceRegistry
from rigbox.plugins.device.transport import TransportOptions
from rigbox.plugins.device.transports.hid_transport import open_hid
from rigbox.plugins.device.transports.replay_transport import open_replay
from rigbox.plugins.device.transports.serial_transport import open_serial
from rigbox.plugins.manifests.api import Codec, parse_values, resolve_manifest

if TYPE_CHECKING:
    from rigbox.core.context import Context


class ConnectArgs(BaseModel):
    """Arguments for `device.connect`."""

    name: str = Field(description="Name to refer to this connection by")
    url: str = Field(description="A serial port (COM3), or a URL whose scheme picks the transport (device.transports)")
    baud: int = Field(115200, ge=1, description="Serial baud rate (ignored by transports without one)")
    timeout: float = Field(1.0, gt=0, allow_inf_nan=False, description="Default read timeout in seconds")
    write_timeout: float | None = Field(
        None,
        gt=0,
        allow_inf_nan=False,
        description="Serial write deadline in seconds (default: --timeout; unsupported by RFC2217 and HID)",
    )
    param: list[str] = Field(
        default_factory=list,
        description="Transport-specific option key=value (repeatable)",
    )


class NameArgs(BaseModel):
    """Arguments for commands that act on one named connection."""

    name: str = Field(description="Connection name")


class DiscoverArgs(BaseModel):
    """Arguments for HID interface discovery."""

    known_only: bool = Field(False, description="Only show HID interfaces matching a plugin-provided device profile")


class ProtocolArgs(NameArgs):
    """Select a message protocol per operation, defaulting to unframed bytes."""

    protocol: str = Field(
        "raw",
        description="Message protocol identifier (device.protocols); omitted means raw",
    )

    @field_validator("protocol")
    @classmethod
    def normalize_protocol(cls, value: str) -> str:
        """Normalize the identifier and reject an empty selection.

        Args:
            value: User-provided protocol name.

        Returns:
            The lowercase identifier.

        Raises:
            ValueError: If no identifier was supplied.
        """
        value = value.strip().lower()
        if not value:
            raise ValueError("protocol identifier must not be empty")
        return value


class SendArgs(ProtocolArgs):
    """Arguments for `device.send`."""

    hex: str | None = Field(None, description="Raw bytes as hex")
    text: str | None = Field(None, description="UTF-8 payload text; the selected protocol adds any framing")
    manifest: str | None = path_field(None, description="Pack a packet with this manifest (needs --values)")
    values: str | None = Field(None, description="Field values, YAML/JSON flow mapping")
    protocol_option: list[str] = Field(
        default_factory=list,
        description="Per-message protocol key=value (repeatable)",
    )


class ExpectArgs(ProtocolArgs):
    """Arguments for `device.expect`."""

    manifest: str | None = path_field(
        None,
        description="Decode payload with this manifest; also sets packet size in raw mode",
    )
    size: int | None = Field(None, ge=1, description="Read exactly this many raw bytes (raw protocol only)")
    timeout: float | None = Field(
        None,
        gt=0,
        allow_inf_nan=False,
        description="Seconds to wait (default: the connection's timeout)",
    )
    payload_format: Literal["hex", "ascii", "utf8"] = Field(
        "hex",
        description="Payload display without a manifest: hex, escaped ASCII, or strict UTF-8",
    )


class RequestArgs(SendArgs):
    """Arguments for `device.request`."""

    frame: str | None = Field(
        None,
        description="Complete protocol frame as hex, before UART escaping; alternative to payload options",
        json_schema_extra={"positional": True},
    )
    response_manifest: str | None = path_field(None, description="Decode response payload; also sets response size in raw mode")
    response_size: int | None = Field(
        None,
        ge=1,
        description="Read exactly this many response bytes (raw protocol only)",
    )
    timeout: float | None = Field(
        None,
        gt=0,
        allow_inf_nan=False,
        description="Response timeout after writing, in seconds (default: connection timeout)",
    )
    payload_format: Literal["hex", "ascii", "utf8"] = Field(
        "hex",
        description="Response payload display without a manifest: hex, escaped ASCII, or strict UTF-8",
    )
    discard_packet: list[str] = Field(
        default_factory=list,
        description="Discard unrelated packets, decimal or hex (repeatable; request only)",
    )


class CaptureArgs(ProtocolArgs):
    """Arguments for `device.capture`."""

    seconds: float = Field(10.0, gt=0, allow_inf_nan=False, description="Stop after this long")
    count: int | None = Field(None, ge=1, description="Stop after this many packets")
    manifest: str | None = path_field(None, description="Decode payloads; also sets packet size in raw mode")
    size: int | None = Field(
        None,
        ge=1,
        description="Split the stream into fixed-size raw packets (raw protocol only)",
    )
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


def _encode(ctx: Context, args: SendArgs) -> bytes:
    if args.hex is not None and args.manifest is None and args.text is None and args.values is None:
        try:
            return bytes.fromhex(args.hex.replace(" ", ""))
        except ValueError as error:
            raise CommandError(f"bad hex: {error}") from None
    if args.text is not None and args.hex is None and args.manifest is None and args.values is None:
        try:
            return args.text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise CommandError(f"cannot encode text as UTF-8: {error}") from None
    if args.manifest is not None and args.values is not None and args.hex is None and args.text is None:
        return Codec(resolve_manifest(ctx, args.manifest)).encode(parse_values(args.values))
    raise CommandError("give either --hex, --text, or --manifest together with --values")


def _packet(ctx: Context, manifest: str | None, size: int | None) -> tuple[Codec | None, int]:
    if manifest is not None and size is None:
        codec = Codec(resolve_manifest(ctx, manifest))
        return codec, codec.size
    if size is not None and manifest is None:
        return None, size
    raise CommandError("give exactly one packet manifest or fixed size")


def _response_packet(ctx: Context, protocol: str, manifest: str | None, size: int | None) -> tuple[Codec | None, int | None]:
    if protocol == "raw":
        return _packet(ctx, manifest, size)
    if size is not None:
        raise CommandError("fixed response size is only supported with --protocol raw; the protocol defines message boundaries")
    return (Codec(resolve_manifest(ctx, manifest)) if manifest is not None else None), None


def _require_message(message: Message | None, conn: Connection, protocol: str) -> Message:
    if message is None:
        raise CommandError(f"timeout waiting for '{protocol}' message on '{conn.name}'; any partial input is retained")
    return message


def _message_options(args: SendArgs) -> dict[str, str]:
    options: dict[str, str] = {}
    for item in args.protocol_option:
        key, equals, value = item.partition("=")
        if not equals or not key or key in options:
            raise CommandError(f"--protocol-option expects a unique key=value, got: {item}")
        options[key] = value
    if options and args.protocol == "raw":
        raise CommandError("raw protocol does not accept per-message options")
    return options


def _message_result(
    data: bytes,
    codec: Codec | None,
    message: Message | None,
    payload_format: Literal["hex", "ascii", "utf8"] = "hex",
) -> RigboxCmdResult:
    if codec is not None:
        text = None
        result = codec.decode(data)
    elif payload_format == "hex":
        text = data.hex()
        result = {"size": len(data), "hex": text}
    elif payload_format == "ascii":
        text = "".join("\\\\" if value == 0x5C else chr(value) if 0x20 <= value <= 0x7E else f"\\x{value:02x}" for value in data)
        result = {"size": len(data), "text": text}
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CommandError(f"cannot decode response payload as UTF-8: {error}") from None
        result = {"size": len(data), "text": text}
    if message is not None and message.metadata:
        # An envelope keeps protocol fields distinct from arbitrary manifest field names.
        return RigboxCmdResult(text=text, structured_data={"metadata": dict(message.metadata), "payload": result})
    return RigboxCmdResult(
        text=text,
        structured_data=result,
    )


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register device commands, byte-stream transports and the line message protocol."""
    devices = registry.extension(DeviceRegistry)
    for scheme in ("serial", "loop", "socket", "rfc2217"):
        devices.add_transport(scheme, open_serial)
    devices.add_transport("replay", open_replay)
    devices.add_transport("hid", open_hid)
    devices.add_protocol("line", open_line)

    @registry.command("device.protocols", record=False)
    def protocols(ctx: Context, args: NoArgs) -> list[dict[str, str]]:
        """List message protocols selectable per send, expect, request or capture operation.

        Args:
            ctx: Current command context.
            args: Unused command arguments.

        Returns:
            Protocol identifiers and factory descriptions; device support is not auto-detected.
        """
        return [{"protocol": "raw", "description": "Unframed bytes (the default)."}] + [
            {
                "protocol": name,
                "description": ((factory.__doc__ or "").strip().splitlines() or [""])[0],
            }
            for name, factory in sorted(ctx.registry.extension(DeviceRegistry).protocols.items())
        ]

    @registry.command("device.transports", record=False)
    def transports(ctx: Context, args: NoArgs) -> list[dict[str, str]]:
        """List available transport URL schemes and how they move bytes.

        Args:
            ctx: Current command context.
            args: Unused command arguments.

        Returns:
            Transport schemes and descriptions of their communication mechanisms.
        """
        serial_descriptions = {
            "serial": "Local serial port via pyserial (COM3 or serial://COM3); --baud sets the line speed.",
            "loop": "In-memory loopback via pyserial (loop://); written bytes become readable without hardware.",
            "socket": "Raw TCP byte stream via pyserial (socket://host:port); no remote serial-port configuration.",
            "rfc2217": "Remote serial port via pyserial (rfc2217://host:port); Telnet/RFC2217 carries data and port settings.",
        }
        return [
            {
                "scheme": scheme,
                "description": (
                    serial_descriptions[scheme]
                    if factory is open_serial and scheme in serial_descriptions
                    else ((factory.__doc__ or "").strip().splitlines() or [""])[0]
                ),
            }
            for scheme, factory in sorted(ctx.registry.extension(DeviceRegistry).transports.items())
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
        devices = ctx.registry.extension(DeviceRegistry)
        params = _parse_params(args.param)
        if args.url.lower() == "hid://" and "device" in params:
            params = select_hid_device(devices.hid_devices, params)
        options = TransportOptions(
            timeout_s=args.timeout,
            baud=args.baud,
            params=params,
            write_timeout_s=args.write_timeout,
        )
        transport = devices.open_transport(args.url, options)
        connection = Connection(args.name, args.url, transport, options, registry=devices, log=ctx.log)
        ctx.connections[args.name] = connection
        return {"name": args.name, "url": args.url, "transport": connection.scheme}

    @registry.command("device.discover", args=DiscoverArgs, record=False)
    def discover(ctx: Context, args: DiscoverArgs) -> list[dict[str, object]]:
        """List connected HID interfaces and matching plugin-provided report profiles.

        Args:
            ctx: Current command context.
            args: Discovery filtering options.

        Returns:
            Interface identity, exact path, matching profile names and declared report layouts.

        Raises:
            CommandError: If HIDAPI enumeration fails.
        """
        return discover_hid(ctx.registry.extension(DeviceRegistry).hid_devices, known_only=args.known_only)

    @registry.command("device.close", args=NameArgs)
    def close(ctx: Context, args: NameArgs) -> None:
        """Close a named connection."""
        _get(ctx, args.name).close()
        del ctx.connections[args.name]

    @registry.command("device.list", record=False)
    def list_(ctx: Context, args: NoArgs) -> list[dict[str, Any]]:
        """List open connections."""
        return [
            {
                "name": c.name,
                "url": c.url,
                "transport": c.scheme,
                "timeout": c.timeout_s,
                "receive": c.receive_stats(),
            }
            for c in ctx.connections.values()
            if isinstance(c, Connection)
        ]

    @registry.command("device.send", args=SendArgs)
    def send(ctx: Context, args: SendArgs) -> dict[str, Any]:
        """Send hex, UTF-8 text or manifest data, optionally framed by a message protocol.

        Args:
            ctx: Current command context.
            args: Connection, payload and per-operation protocol selection.

        Returns:
            The transmitted wire byte count and hex, plus the protocol when non-raw.
        """
        conn = _get(ctx, args.name)
        data = _encode(ctx, args)
        options = _message_options(args)
        if args.protocol == "raw":
            conn.write(data)
        else:
            data = conn.send_message(data, args.protocol, options=options)
        result: dict[str, Any] = {
            "name": args.name,
            "sent": len(data),
            "hex": data.hex(),
        }
        if args.protocol != "raw":
            result["protocol"] = args.protocol
        return result

    @registry.command("device.expect", args=ExpectArgs)
    def expect(ctx: Context, args: ExpectArgs) -> RigboxCmdResult:
        """Receive a raw fixed-size packet or one complete protocol message.

        Args:
            ctx: Current command context.
            args: Connection, optional payload manifest and response selection.

        Returns:
            Decoded manifest fields or the received payload size and selected hex/text display.

        Raises:
            CommandError: If selection, framing, decoding or communication fails or times out.
        """
        conn = _get(ctx, args.name)
        if args.manifest is not None and args.payload_format != "hex":
            raise CommandError("--payload-format cannot be combined with --manifest")
        codec, size = _response_packet(ctx, args.protocol, args.manifest, args.size)
        timeout_s = args.timeout if args.timeout is not None else conn.timeout_s
        message: Message | None = None
        if size is not None:
            data = conn.read_exactly(size, timeout_s)
        else:
            try:
                message = _require_message(conn.receive_message(args.protocol, timeout_s), conn, args.protocol)
            except EOFError:
                raise CommandError(f"stream ended waiting for '{args.protocol}' message on '{conn.name}'") from None
            data = message.payload
        return _message_result(data, codec, message, args.payload_format)

    @registry.command("device.request", args=RequestArgs)
    def request(ctx: Context, args: RequestArgs) -> RigboxCmdResult:
        """Send one request and receive its response without interleaved connection I/O.

        Raw requests require --response-size or --response-manifest. A protocol defines response boundaries;
        line returns the next line, not a correlated reply. An optional positional hex frame is validated by the
        protocol instead of constructing a frame from payload options. Input is retained and writes are never retried.

        Args:
            ctx: Current command context.
            args: Connection, request payload and response/protocol selection.

        Returns:
            Decoded manifest fields or the response payload size and selected hex/text display.

        Raises:
            CommandError: If selection, framing, decoding or communication fails or times out.
        """
        conn = _get(ctx, args.name)
        if args.response_manifest is not None and args.payload_format != "hex":
            raise CommandError("--payload-format cannot be combined with --response-manifest")
        codec, size = _response_packet(ctx, args.protocol, args.response_manifest, args.response_size)
        if args.frame is not None:
            if any(value is not None for value in (args.hex, args.text, args.manifest, args.values)):
                raise CommandError("do not combine a complete frame with --hex, --text, --manifest or --values")
            if args.protocol == "raw":
                raise CommandError("complete frames require a supporting protocol; use --hex for raw requests")
            try:
                payload = bytes.fromhex(args.frame)
            except ValueError as error:
                raise CommandError(f"bad frame hex: {error}") from None
        else:
            payload = _encode(ctx, args)
        options = _message_options(args)
        if args.discard_packet:
            if "discard_packets" in options:
                raise CommandError("do not combine --discard-packet with --protocol-option discard_packets")
            if any("," in rule for rule in args.discard_packet):
                raise CommandError("--discard-packet expects one TYPE[:COMMAND_ID] rule; repeat the option for more")
            options["discard_packets"] = ",".join(args.discard_packet)
        timeout_s = args.timeout if args.timeout is not None else conn.timeout_s
        message: Message | None = None
        if size is not None:
            data = conn.request(payload, size, timeout_s)
        else:
            try:
                message = _require_message(
                    conn.request_message(payload, args.protocol, timeout_s, options=options, framed=args.frame is not None),
                    conn,
                    args.protocol,
                )
            except EOFError:
                raise CommandError(f"stream ended waiting for '{args.protocol}' response on '{conn.name}'") from None
            data = message.payload
        return _message_result(data, codec, message, args.payload_format)

    @registry.command("device.capture", args=CaptureArgs)
    def capture(ctx: Context, args: CaptureArgs) -> dict[str, Any]:
        """Record raw packets or complete protocol messages as timestamped NDJSON.

        Args:
            ctx: Current command context.
            args: Connection, framing/decoding selection and capture limits.

        Returns:
            The capture path and packet/error counts.

        Raises:
            CommandError: If options, protocol framing or communication fail.
        """
        conn = _get(ctx, args.name)
        if args.manifest and args.size:
            raise CommandError("give at most one of --manifest or --size")
        if args.protocol != "raw" and args.size is not None:
            raise CommandError("--size is only supported with --protocol raw; the protocol defines message boundaries")
        codec = Codec(resolve_manifest(ctx, args.manifest)) if args.manifest else None
        size = (codec.size if codec else args.size) if args.protocol == "raw" else None
        conn.check_protocol(args.protocol)
        path = _capture_path(ctx, args.name, args.out)
        path.parent.mkdir(parents=True, exist_ok=True)

        end = time.monotonic() + args.seconds
        packets = errors = 0
        with path.open("w", encoding="utf-8") as f:
            while args.count is None or packets < args.count:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                wait = min(conn.timeout_s, remaining)
                message: Message | None = None
                try:
                    if args.protocol == "raw":
                        data = conn.read(size, wait) if size else conn.read_chunk(wait)
                        if not data:
                            continue
                    else:
                        message = conn.receive_message(args.protocol, wait)
                        if message is None:
                            continue
                        data = message.payload
                except EOFError:  # a finished recording: nothing more will ever arrive
                    break
                record: dict[str, Any] = {
                    "ts": datetime.now(UTC).isoformat(),
                    "dir": "rx",
                    "hex": message.wire_data.hex() if message is not None else data.hex(),
                }
                if message is not None:
                    record.update(protocol=args.protocol, payload_hex=data.hex())
                    if message.metadata:
                        record["metadata"] = dict(message.metadata)
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
