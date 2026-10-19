# Devices

Connections are named and last for a REPL session or one workflow run. A connection URL's scheme picks the
**transport** (list them with `device.transports`):

| URL | Transport |
|---|---|
| `COM3`, `/dev/ttyUSB0`, `serial://COM3` | a serial port (pyserial; `--baud` sets the speed) |
| `loop://` | a loopback port that echoes what you send, to try everything without hardware |
| `socket://host:port`, `rfc2217://host:port` | serial over TCP |
| `replay://captures\run1.ndjson` | plays a `device.capture` file back as the incoming data (writes are ignored); reads fail with "stream ended" when it runs out |

Other kinds of device (Bluetooth, a vendor SDK, a debug probe) are added by a plugin that registers a scheme; see
[Writing a transport](#writing-a-transport) below.

Run these examples from the repository root:

```
rigbox> device.ports
rigbox> device.connect dut loop://
rigbox> device.send dut --manifest examples\sensor_report.yaml --values "{id: 1, temperature: 2.5, ok: true, label: n1, samples: [1,2,3,4], blob: dead}"
rigbox> device.expect dut --manifest examples\sensor_report.yaml
rigbox> device.request dut --hex "01 02" --response-size 2 --timeout 1
rigbox> device.capture dut --manifest examples\sensor_report.yaml --seconds 10 --count 50
rigbox> device.close dut
```

`device.capture` writes timestamped NDJSON (`ts`, `dir`, `hex`, `decoded` or `error`) to the workspace `captures/`
folder. Sessions assemble partial reads into manifest-sized (or `--size`) packets using one overall read deadline.
Timeouts and end-of-stream errors include any partial bytes. Variable-length framing (COBS, SLIP, length prefixes)
is not implemented; incomplete packets are consumed, not buffered for the next command.

`device.request` uses the same `--hex` or `--manifest`/`--values` inputs as `device.send`. Select the response with
exactly one of `--response-size` or `--response-manifest`; a response manifest also decodes it. The write and reply
read are serialized as one session operation. The response `--timeout` starts after writing; lock acquisition has
its own wait bounded by that timeout. Buffered input is retained, so this is not response correlation. Failed writes
are never automatically retried.

For serial, loopback and socket transports, writes default to the connection's `--timeout`; set `--write-timeout`
on `device.connect` to use a separate deadline. Writes do not flush or wait for device acknowledgement. Pyserial's
RFC2217 handler does not support configurable write deadlines: it keeps its native behavior and rejects an explicit
`--write-timeout`. Context shutdown attempts every connection's cleanup and reports failures on stderr.

Replaying a capture lets you rehearse the rest of the toolbox without hardware:

```
rigbox> device.connect rec replay://captures\run1.ndjson
rigbox> device.capture rec --manifest examples\sensor_report.yaml --out again.ndjson   # stops when the recording ends
```

## Writing a transport

A transport is any object with `write(data)`, `read(size, timeout)`, `read_chunk(timeout)` and `close()` (the
`Transport` protocol in [`core/transport.py`](../../core/transport.py)). Reads may return partial data; return empty bytes on timeout,
raise `EOFError` when the stream has permanently ended, and raise `OSError` on communication failure. Writes must
send every byte or raise, and cleanup should be idempotent. A plugin registers a factory for a scheme, and users
then connect with `<scheme>://...`:

```python
from rigbox.core.registry import Registry
from rigbox.core.transport import Transport, TransportOptions
from rigbox.hooks import hookimpl


def open_mydevice(url: str, options: TransportOptions) -> Transport:
    """My device over its SDK: mydevice://serial-number."""   # shown by `device.transports`
    return MyDeviceTransport(url.partition("://")[2], options.timeout, options.params)


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `mydevice` transport."""
    registry.add_transport("mydevice", open_mydevice)
```

`options.params` carries any `--param key=value` the user passed to `device.connect`.
`Connection` owns packet assembly, user-facing I/O errors, locking and cleanup, and can also be used as a Python
context manager. Keep framing and codecs out of transport implementations. HID reports or BLE notifications need
an explicit stream adaptation that preserves their protocol's identifiers and boundaries; they are not automatically
interchangeable byte streams. No Rigbox background reader threads or queues are introduced.