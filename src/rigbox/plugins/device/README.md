# Devices

The device plugin opens named connections and provides `device.send`, `device.expect`, `device.request`,
`device.capture`, and connection management. List communication channels with `device.transports` and message
formats with `device.protocols`. Manifests encode/decode application payloads independently of protocol framing.

Connections use a URL to choose a **transport**:

| URL | Transport |
|---|---|
| `COM3`, `/dev/ttyUSB0`, `serial://COM3` | Serial port (`--baud` sets the speed) |
| `loop://` | Loopback port that echoes sent data |
| `socket://host:port`, `rfc2217://host:port` | Serial over TCP |
| `replay://captures\run1.ndjson` | Replays incoming data from a `device.capture` file; writes are ignored |

## What is a transport?

A transport adapts a communication channel to Rigbox's byte-stream interface: write bytes, read bytes with a timeout,
and close the connection. It handles communication with the device, not packet framing, manifest decoding, or
request/response coordination. Those responsibilities stay in the device session and higher-level commands.

Reads may return partial data. Return `b""` on timeout, raise `EOFError` when the stream has permanently ended, and
raise `OSError` on communication failure. Both `read` and `read_chunk` timeouts are in seconds. Writes must send all
bytes or raise; cleanup should be safe to call more than once. See the [`Transport` protocol](transport.py).

The device subsystem lives entirely in this plugin: transport/protocol contracts, their factory registry,
connections, implementations and commands. Core owns command dispatch, plugin discovery and generic resource
cleanup; it has no device-specific registration API.

## Adding device support

Choose the extension point that matches what is new:

- **A new communication scheme or interface** (for example, a vendor SDK that exposes a byte stream): implement a
  transport and register its URL scheme, as shown below. Existing `device.*` commands can then use it.
- **A message protocol**: register a protocol session factory (below) for framing and response coordination.
  Select it per operation with `--protocol`; no explicit attach step is needed.
- **A particular device's operations**: use manifests for payload layouts and plugin commands for device-specific
  workflows. If the device is controlled through
  a structured SDK rather than a byte stream, add a plugin with commands that use that SDK directly instead of
  forcing it into a transport.

Plugins can be placed in `<workspace>/plugins/*.py` or packaged as an installed `rigbox.plugins` entry point. Create
a workspace plugin scaffold with `rigbox run plugin.new <name>`.

## Implementing a transport

Implement the [`Transport` protocol](transport.py), then register a factory with
`DeviceRegistry.add_transport`. The factory receives the full URL and `TransportOptions`; `params` contains values passed
with `device.connect --param key=value`.

```python
from rigbox.core.registry import Registry
from rigbox.hooks import hookimpl
from rigbox.plugins.device.registry import DeviceRegistry
from rigbox.plugins.device.transport import Transport, TransportOptions


def open_mydevice(url: str, options: TransportOptions) -> Transport:
    """Open a connection to a device using my SDK."""
    return MyDeviceTransport(url.partition("://")[2], options)


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the mydevice URL scheme."""
    registry.extension(DeviceRegistry).add_transport("mydevice", open_mydevice)
```

Users can then connect with a URL such as `mydevice://serial-number`. `Connection` owns packet assembly, locking,
user-facing I/O errors, and cleanup; it can also be used as a Python context manager. Keep framing and codecs out of
transports. HID reports or BLE notifications need an explicit stream adaptation that preserves protocol identifiers
and boundaries. Raw and legacy protocol operations remain synchronous. Protocols implementing
`ContinuousProtocol.prepare_request` opt into a connection-owned continuous reader.

`registry.extension(DeviceRegistry)` gets or creates one shared device registry for that command registry.
This lets a plugin register before or after the device plugin's hook; registrations are not global and are not
replaced when built-ins load. The extension constructor does not open devices or install built-in factories.
Python plugins must import contracts from `rigbox.plugins.device.transport` / `rigbox.plugins.device.protocol`
and register through this extension, replacing the former `rigbox.core.*` imports and direct
`registry.add_transport` / `registry.add_protocol` calls. Device commands and their arguments are unchanged.

## Message protocols

Connect over a transport, then choose the protocol **per operation**:

```text
rigbox> device.connect console loop://
rigbox> device.send console --protocol line --text "version"
rigbox> device.expect console --protocol line
rigbox> device.request console --protocol line --text "status"
rigbox> device.capture console --protocol line --seconds 2 --out lines.ndjson
rigbox> device.close console
```

Omitting `--protocol` (or selecting `raw`) keeps the existing fixed-size/raw-chunk behavior. `--text` sends UTF-8
payload bytes; `--hex` and `--manifest`/`--values` remain available. Send results describe **wire bytes**, including
framing. Expect/request results describe the **payload**, without framing, as size/hex or decoded manifest fields.
With a non-raw protocol, omit `--size`/`--response-size`; the protocol determines the boundary. A receive manifest
decodes and validates the framed payload rather than controlling how many bytes to read.

The built-in `line` protocol adds LF on send and removes it from received payloads. Supply one payload without LF;
embedded LF is rejected. CR is preserved, so CRLF peers produce payloads ending in CR. Empty lines are messages,
not timeouts. The session retains partial messages after timeouts and additional lines from coalesced reads.
The maximum outgoing wire message and total retained input buffer are each 64 KiB. Input overflow fails the
session explicitly and requires closing/reconnecting; truncated EOF is also an error, not a completed message.
There is no idle-gap detection, prompt matching or response correlation: a line request returns the next line,
including previously queued input.

The connection owns the transport and exclusive access lock. A protocol session is created internally when first
selected and reused for subsequent operations using that protocol. Switching to another protocol, including raw,
is allowed only when the current session has no pending input or framing failure. Otherwise drain using the same
protocol or close/reconnect; input is never silently discarded. Closing is always allowed.
This checks **already buffered** input, not bytes still on the device or in the transport's receive buffer.
Per-operation selection does not enable multiplexing: mixing protocols still requires firmware support and
explicit routing or mode switching. Rigbox does not auto-detect device/command support.

Protocol captures keep exact wire bytes in `hex`, plus `protocol` and `payload_hex`; optional `decoded` fields
describe the payload. Thus the same file can be replayed and parsed with the original protocol. Clean EOF ends
capture; framing/transport errors fail the command, leaving the records already written. Incomplete input at a
capture timeout remains in the session for a subsequent operation.

### Implementing a protocol

Implement [`ProtocolSession`](protocol.py) and register its factory:

```python
from rigbox.core.registry import CommandError, Registry
from rigbox.hooks import hookimpl
from rigbox.plugins.device.protocol import ProtocolSession
from rigbox.plugins.device.registry import DeviceRegistry


def open_myprotocol(scheme: str) -> ProtocolSession:
    """Open my device's message protocol."""
    if scheme not in {"serial", "loop", "replay"}:
        raise CommandError(f"myprotocol does not support {scheme}")
    return MyProtocolSession()


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register a per-operation message protocol."""
    registry.extension(DeviceRegistry).add_protocol("myprotocol", open_myprotocol)
```

The factory receives the connection's transport scheme and can reject unsupported combinations. Identifiers
are case-insensitive; `raw` is reserved. Sessions implement `encode`, `receive`, `request`, and `pending_input`.
They receive a restricted `MessageChannel` interface under the connection lock and must not open/close the
transport or start competing readers. `receive`/`request` return a `Message(payload, wire_data, metadata)` or `None` on
timeout (not `b""`, since empty payloads are valid). Keep exact wire bytes for replay, bound retained input,
use one overall read deadline, retain unrelated messages, and never retry a write automatically.
`metadata` is optional and contains named integer/string protocol fields; results envelope payloads only when
metadata is present. `encode` and `request` accept keyword-only `options: Mapping[str, str] | None = None`;
validate these before I/O and reject unsupported fields. Existing implementations without this parameter
still work when no options are supplied, but must adopt it to accept per-message options.
`request` owns protocol-specific matching and completion; its response timeout starts after writing.
For continuous reception, also implement `prepare_request(payload, *, options=None) -> RequestPlan`:
return validated wire bytes and typed `matches(Message)` / `discard(Message)` predicates without I/O.
The connection starts its sole reader on the first framed operation for that session. The worker calls
`receive` against buffered input under a parser lock and routes matching responses separately from a bounded
unsolicited queue (256 packets / 256 KiB of wire bytes, drop oldest with diagnostics/counters on overflow).
Previously queued messages never satisfy a new request; partial input stays with the persistent parser.
Protocol diagnostics are bounded/collected by the worker and emitted on the foreground thread.
`device.list` exposes queue/loss counters under `receive`. Failed readers propagate explicit errors.
Protocol switching stops/joins the worker and refuses to abandon queued/partial/failed/unresolved state.
Closing also joins the worker; transports must honor read deadlines. No second transport reader is allowed.
Without this optional capability, existing sessions continue using their synchronous `request` implementation.
The workspace RACE plugin opts in; raw and built-in line behavior is unchanged.
Replay remains synchronous/consumer-paced even for opted-in protocols, avoiding artificial queue losses
when a finite recording is read much faster than a live device.
Use `CommandError` for framing/user errors, propagate `OSError` for I/O failure, and use `EOFError` only for
clean stream termination. Configurable delimiters, HID adaptation, command-spec loading and COBS/SLIP remain
future work.

## Examples

Run these from a workspace:

```text
rigbox> device.ports
rigbox> device.connect dut loop://
rigbox> device.send dut --manifest examples\sensor_report.yaml --values "{id: 1, temperature: 2.5, ok: true, label: n1, samples: [1,2,3,4], blob: dead}"
rigbox> device.expect dut --manifest examples\sensor_report.yaml
rigbox> device.request dut --hex "01 02" --response-size 2 --timeout 1
rigbox> device.capture dut --manifest examples\sensor_report.yaml --seconds 10 --count 50
rigbox> device.close dut
```

In raw mode, `device.capture` writes timestamped NDJSON to the workspace's `captures/` folder. Reads assemble partial data into
manifest-sized (or `--size`) packets using one overall deadline. Incomplete packets are consumed rather than buffered
for the next command.

Raw `device.request` accepts the same request input as `device.send`. Select its response with exactly one of
`--response-size` or `--response-manifest`. The write and reply read are serialized, but buffered input is retained,
so replies are not correlated. Failed writes are never retried automatically.

Serial, loopback, and socket writes use the connection's `--timeout` by default. `--write-timeout` sets a separate
deadline where supported. Writes do not wait for device acknowledgement. RFC2217 does not support configurable write
deadlines and retains pyserial's native behavior. Context shutdown attempts to close every connection and reports
failures on stderr.

Replay a capture without hardware:

```text
rigbox> device.connect rec replay://captures\run1.ndjson
rigbox> device.capture rec --manifest examples\sensor_report.yaml --out again.ndjson
```
