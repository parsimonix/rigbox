# Devices

The device plugin opens named connections and provides `device.send`, `device.expect`, `device.request`,
`device.capture`, and connection management. It can encode and decode packets with manifests. List available
connection types with `device.transports`.

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
bytes or raise; cleanup should be safe to call more than once. See the [`Transport` protocol](../../core/transport.py).

## Adding device support

Choose the extension point that matches what is new:

- **A new communication scheme or interface** (for example, a vendor SDK that exposes a byte stream): implement a
  transport and register its URL scheme, as shown below. Existing `device.*` commands can then use it.
- **A particular device's protocol or operations**: if it fits the byte-stream model, use manifests for its packet
  layout and add plugin commands for device-specific workflows or convenience. If the device is controlled through
  a structured SDK rather than a byte stream, add a plugin with commands that use that SDK directly instead of
  forcing it into a transport.

Plugins can be placed in `<workspace>/plugins/*.py` or packaged as an installed `rigbox.plugins` entry point. Create
a workspace plugin scaffold with `rigbox run plugin.new <name>`.

## Implementing a transport

Implement the [`Transport` protocol](../../core/transport.py), then register a factory with
`Registry.add_transport`. The factory receives the full URL and `TransportOptions`; `params` contains values passed
with `device.connect --param key=value`.

```python
from rigbox.core.registry import Registry
from rigbox.core.transport import Transport, TransportOptions
from rigbox.hooks import hookimpl


def open_mydevice(url: str, options: TransportOptions) -> Transport:
    """Open a connection to a device using my SDK."""
    return MyDeviceTransport(url.partition("://")[2], options)


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the mydevice URL scheme."""
    registry.add_transport("mydevice", open_mydevice)
```

Users can then connect with a URL such as `mydevice://serial-number`. `Connection` owns packet assembly, locking,
user-facing I/O errors, and cleanup; it can also be used as a Python context manager. Keep framing and codecs out of
transports. HID reports or BLE notifications need an explicit stream adaptation that preserves protocol identifiers
and boundaries. Rigbox does not add background reader threads or queues.

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

`device.capture` writes timestamped NDJSON to the workspace's `captures/` folder. Reads assemble partial data into
manifest-sized (or `--size`) packets using one overall deadline. Incomplete packets are consumed rather than buffered
for the next command; variable-length framing (COBS, SLIP, length prefixes) is not implemented.

`device.request` accepts the same request input as `device.send`. Select its response with exactly one of
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
