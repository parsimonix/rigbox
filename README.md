# rigbox

A manifest-driven REPL and CLI toolbox for embedded and DSP developers.

## Setup

Requires Python 3.11 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check    # lint
.\.venv\Scripts\ty.exe check      # type check
```

## Use

```powershell
rigbox init my-project        # create a workspace (rigbox.yaml)
rigbox                        # interactive shell (run from inside a workspace)
rigbox run commands           # list the commands (add --prefix dsp. to narrow it)
rigbox run dsp.gen --help     # one command's arguments
rigbox run --format json demo.echo hi --times 2
rigbox script workflows\x.yaml --var key=value
```

Output is a human message (or a table for data-only commands) on a terminal and NDJSON when piped.
Select `--format human`, `table`, `json`, or `ndjson` explicitly when needed.

### The shell

The shell is a [cmd2](https://github.com/python-cmd2/cmd2) app; every rigbox command is a shell command with the same
arguments and `--help` as `rigbox run`. Tab completes command names, option names, choices and file names. cmd2's own
commands work too:

```
rigbox> help                           # commands grouped by area; `help dsp.gen` or `dsp.gen --help` for one
rigbox> dsp.gen sine tone.wav --seconds 1
rigbox> set format json                # print results as JSON (human, table, json, ndjson, auto)
rigbox> alias create tone dsp.gen sine # then: tone out.wav
rigbox> device.transports | findstr replay      # pipe to a shell command
rigbox> dsp.inspect tone.wav > report.txt       # redirect to a file
rigbox> history                        # what you typed (kept in .rigbox/ inside a workspace)
rigbox> exit                           # or quit, or Ctrl-D
```

The bar under the prompt shows the workspace, open device connections and output format.

Commands you run are recorded as structured steps; `workflow.save <name> --last N` turns the last N into a shareable
YAML workflow in `workflows/`, and `workflow.run <name>` replays it.

## Manifests

A manifest is a YAML description of a packed binary packet (see `examples/sensor_report.yaml`). Field types:
`u8..u64`, `i8..i64`, `f32`, `f64`, `bool`, `string`/`bytes` (need `length`), and `count` for numeric arrays.

```powershell
rigbox run manifest.show examples\sensor_report.yaml     # packed layout: offset and size per field
rigbox run manifest.lint examples\sensor_report.yaml     # style findings
rigbox run manifest.diff old.yaml new.yaml               # flags wire-breaking changes
rigbox run manifest.generate examples\sensor_report.yaml # writes sensor_report_model.py (pydantic)
rigbox run manifest.encode examples\sensor_report.yaml "{id: 1, temperature: 2.5, ok: true, label: n1, samples: [1,2,3,4], blob: dead}"
rigbox run manifest.decode examples\sensor_report.yaml <hex>
```

Inside a workspace, manifests can be referenced by name from `manifests/`.

## Devices

Connections are named and last for a REPL session or one workflow run. A connection URL's scheme picks the
**transport** (list them with `device.transports`):

| URL | Transport |
|---|---|
| `COM3`, `/dev/ttyUSB0`, `serial://COM3` | a serial port (pyserial; `--baud` sets the speed) |
| `loop://` | a loopback port that echoes what you send, to try everything without hardware |
| `socket://host:port`, `rfc2217://host:port` | serial over TCP |
| `replay://captures\run1.ndjson` | plays a `device.capture` file back as the incoming data (writes are ignored); reads fail with "stream ended" when it runs out |

Other kinds of device (Bluetooth, a vendor SDK, a debug probe) are added by a plugin that registers a scheme; see
"Writing a transport" below.

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

### Writing a transport

A transport is any object with `write(data)`, `read(size, timeout)`, `read_chunk(timeout)` and `close()` (the
`Transport` protocol in `rigbox/core/transport.py`). Reads may return partial data; return empty bytes on timeout,
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

## State machines

A state machine manifest lists states, events and guarded transitions (see `examples/power_mode.yaml`). Guards and
event `match` expressions are safe expressions over data, such as `temperature > 90 and mode == "running"`.

```powershell
rigbox run sm.check examples\power_mode.yaml      # structural findings, including closed cycles and required events
rigbox run sm.show examples\power_mode.yaml       # transition table
rigbox run --format ndjson sm.render examples\power_mode.yaml   # Mermaid text (or --out file.mmd)
rigbox run sm.simulate examples\power_mode.yaml "[start, {overheat: {temperature: 95}}, reset]"
rigbox run sm.replay examples\power_mode.yaml captures\run1.ndjson --only-problems
rigbox run codegen.state_machine examples\power_mode.yaml   # C header with state and event enums
```

`sm.check` returns a `findings` list whose entries contain `level`, `rule`, `subject`, and `message`. If there are none,
the list is empty and human output reports success (green in Rich mode). It ignores guards: a path or
declared transition is not proof that its conditions can be satisfied. Existing severities are unchanged.
`trapping-component` warns about a reachable closed cycle with no safe state, excluding the component containing
the initial state (the machine's entire operating cycle). Dead ends are reported separately.

`codegen.state_machine` writes a C header containing declaration-order state and event enums. It does not yet generate
transition execution, guards, or action implementations. Existing output files are protected unless `--force` is set.

To require an event handler in a state, add `required_events` to that state's declaration:

```yaml
- name: running
    required_events: [stop, trip]
```

Names must refer to declared events. Missing outgoing transitions produce `missing-required-event` errors;
wildcard and list sources count, as do guarded transitions. This checks declaration coverage, not guard coverage.
Events not listed remain optional. Findings are returned as data; they do not change the command's exit status.

Coverage and fuzzing:

```powershell
rigbox run sm.fuzz examples\power_mode.yaml --seed 1 --runs 200 --length 30 --coverage-file cov.json
rigbox run sm.simulate examples\power_mode.yaml "[start]" --coverage-file cov.json   # accumulates into the same file
rigbox run sm.coverage examples\power_mode.yaml cov.json                              # what is still uncovered
```

`sm.simulate`, `sm.replay` and `sm.fuzz` track which states, transitions and guard outcomes (true and false) were
covered and log a one-line summary; `--coverage-file` merges the run into a JSON file you can inspect with
`sm.coverage`. `sm.fuzz` takes random events with boundary-value data (around the constants in your guards, bounded by
`--packet` if given) and fails, with a ready-to-paste `sm.simulate` reproduction, if an `invariants:` expression
breaks or a guard can't be evaluated. The seed is always printed, so any run can be repeated.

`sm.replay` takes a `device.capture` file, turns each decoded packet into an event using the events' `match`
expressions, and reports where the device's own state (the `state_field` packet field) differs from the model.
After a divergence the model adopts the device's state, so later mismatches stand alone.

State-machine commands provide plaintext details, Rich output, and structured envelopes such as `transitions`,
`findings`, `steps`, and `coverage`. JSON returns an array containing the envelope; NDJSON emits one aggregate record,
not one line per nested item. Replay returns `rows` plus `records`, `diverged`, `skipped`, and `errors` totals before
filtering. `--only-problems` retains divergences, skipped records, and event-processing errors. Divergences and skipped
records produce warning-level results; processing errors produce error-level results without changing the exit status.

## DSP

Signals are files (`.wav`, `.csv`, `.npz`, or headerless `.raw`/`.bin` dumps), so every command works the same in the
REPL, one-shot runs and workflows. Samples are floats with full scale = 1.0; integer files map as value / 2^(bits-1),
so int16 is Q15 and int32 is Q31. Out-of-range samples are clipped and the clip count is reported.

```powershell
rigbox run dsp.gen sine tone.wav --freq 1000 --seconds 1 --amp 0.5    # sine square sawtooth triangle noise gaussian sweep impulse step dc
rigbox run dsp.inspect tone.wav                  # peak/rms/dBFS, clipping, fundamental, THD, THD+N
rigbox run dsp.spectrum tone.wav --peaks 3 --plot spectrum.png         # --out spectrum.csv for the whole curve
rigbox run dsp.resample tone.wav tone16k.wav 16000
rigbox run dsp.convert dump.raw dump.wav --fs 16000 --dtype int16 --channels 2 --layout planar
rigbox run dsp.convert tone.wav tone.raw --out-dtype int32 --endian big
```

Filters are YAML manifests (see `examples/lowpass_1k.yaml`): IIR (butterworth, chebyshev1/2, elliptic, bessel; stored as
second-order sections) or windowed FIR. `cutoff` means the -3 dB point for butterworth, the passband edge for
chebyshev1/elliptic, the stopband edge for chebyshev2, and the -6 dB point for FIR. `numeric` (`float32`, `q15`, `q31`) is
the format the filter will run in.

```powershell
rigbox run dsp.filter.design examples\lowpass_1k.yaml                  # coefficients
rigbox run dsp.filter.response examples\lowpass_1k.yaml --plot r.png   # gains, -3 dB corners, stability
rigbox run dsp.filter.apply examples\lowpass_1k.yaml in.wav out.wav    # filter a signal file
rigbox run dsp.filter.export examples\lowpass_1k.yaml --target cmsis   # lowpass_1k.h next to the manifest
rigbox run dsp.filter.export examples\lowpass_1k.yaml --format json --numeric float32
rigbox run dsp.filter.estimate examples\lowpass_1k.yaml --cycles-per-mac 1 --clock-mhz 120
```

Export writes C, JSON or CSV in a `generic` layout (conventional coefficient signs) or the `cmsis` layout for
`arm_biquad_cascade_df1_*` / `arm_fir_*` (negated feedback terms, q15 zero padding, shared post-shift, reversed FIR taps).
For q15/q31 it reports the worst-case magnitude-response error against the float design and whether the quantized poles
are still stable. Plots need the optional extra: `pip install "rigbox[plot]"`.

The estimator uses the cycles-per-MAC and clock you give it (there is no built-in table of cores). It assumes direct
form I biquads (5 MACs per stage) and a plain FIR tap loop.

## Adding commands

A plugin is a module with one hook function. The quickest start is to scaffold one inside a workspace:

```powershell
rigbox run plugin.new greeter     # writes plugins/greeter.py with an example command (restart rigbox to load it)
rigbox run greeter.hello --who bench
```

Or write it by hand and drop it in `<workspace>/plugins/` (or ship it as an entry point in the `rigbox.plugins`
group):

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rigbox.core.registry import CommandResult, Registry
from rigbox.hooks import hookimpl

if TYPE_CHECKING:
    from rigbox.core.context import Context


class HelloArgs(BaseModel):
    """Arguments for `greeter.hello`."""

    who: str = Field("world", description="Who to greet")


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `greeter.*` commands."""

    @registry.command("greeter.hello", args=HelloArgs, record=False)
    def hello(ctx: Context, args: HelloArgs) -> CommandResult:
        """Greet someone."""
        return {"greeting": f"hello, {args.who}"}
```

Commands take a pydantic args model and may return a `RigboxResponse` containing structured data and an optional
`HumanReadableResponse(text, type)` message (both from `rigbox.core.render`). Plain dict/model/list results remain
supported and fall back to tables in human mode. `Context.run(...)` returns a response, not a list; access
`.structured_data` for normalized rows. Raise `CommandError` for user mistakes.
Contributors adding to the repo itself can use the project skills in `.claude/skills/`
(`onboard-me`, `add-plugin`, `add-command`).

## Layout

- `src/rigbox/core/`: framework (registry, parsing, rendering, workspace, workflows, plugin loading)
- `src/rigbox/plugins/`: built-in plugins
- `docs/decisions.md`: architecture decisions and rationale
- `CLAUDE.md`: conventions and preferences for contributors and Claude Code
