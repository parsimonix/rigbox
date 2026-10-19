# Architecture decisions

| # | Decision | Choice | Why |
|---|----------|--------|-----|
| 1 | Command model | Custom registry; each command declares a pydantic args model and returns pydantic result(s) | REPL, CLI and scripted mode share one dispatch path; completion, help and arg parsing derive from the schema |
| 2 | Manifest format | YAML (safe loader + pydantic validation) | Readable for nested structs / state machines, supports comments, diffs well |
| 3 | Workflows | Declarative YAML steps (`run` + `args` + `${var}` substitution) | Safe to share, diffable, can be recorded from session history. Python escape hatch = a plugin command |
| 4 | Plugin discovery | Entry points (`rigbox.plugins`) + workspace-local `plugins/*.py` | Domain areas ship as installable plugins; teams can drop in project-specific verbs |

## Buy, don't build

Use established packages for non-core functionality; keep any home-grown layer a thin shim.

| Area | Package | Status |
|------|---------|--------|
| Plugins | `pluggy` (pytest-dev, MIT) | Adopted. Contract in `src/rigbox/hooks.py`; discovery shim in `core/plugins.py` |
| Models / validation | `pydantic` | Adopted |
| YAML | `pyyaml` | Adopted. Consider `ruamel.yaml` if manifest *editing* must preserve comments |
| Interactive shell | `cmd2` (>=4.3,<5; MIT; built on prompt_toolkit and rich) | Adopted; see "Shell and command-line parsing" below |
| Binary pack/unpack | `construct` (Bulski/Filiba, MIT) | Adopted. `plugins/manifests/build.py` compiles a manifest to a `construct.Struct` |
| Numerics / DSP | `numpy`, `scipy.signal` (filter design, resampling, spectra), `scipy.io.wavfile` | Adopted. Only gaps filled by us: Q-format quantization, CMSIS layout, resource estimate |
| Plots | `matplotlib` | Optional extra (`rigbox[plot]`), imported lazily, Agg backend (PNG files only) |
| State machine engine | `transitions` (pytransitions, MIT) | Adopted. `plugins/statemachine/engine.py` builds a Machine from the manifest |
| State machine graph analysis | `networkx` (NetworkX Developers, BSD-3-Clause) | Adopted. Shared `MultiDiGraph` indexes and established reachability / strongly connected component algorithms; no custom graph engine |
| Guard/match expressions | `simpleeval` (MIT) | Adopted. Safe expression evaluation, no arbitrary code |
| Serial / transports | `pyserial` (Chris Liechti, BSD) | Adopted. Its URL handlers give us `loop://` (mock) and `socket://` for free |
| Table output | `rich` (Will McGugan, MIT) | Adopted behind `render()` in `core/render.py` |
| Code templates | `jinja2` (Pallets, BSD-3-Clause) | Already a dependency; use strict packaged templates with typed contexts for generated source |

Audit of remaining home-grown core (recommendations, not yet applied):

| Code | Size | Recommendation |
|------|------|----------------|
| `core/parse.py` arg parsing | ~90 lines | **Replaced** by `core/cli_parser.py` (argparse parsers generated from the args models) plus cmd2. An earlier spike of click 8.5 + click-repl 0.4.1 + pydanclick 0.5.1 was rejected: pydanclick makes every argument an `--option`, was last released Feb 2025, and click-repl splits lines with POSIX shlex so unquoted Windows paths lose their backslashes |
| `core/registry.py` | ~80 lines | Keep; pluggy hooks are not a command registry, and typer would only pay off if we adopted it for parsing too |
| `core/history.py` | ~30 lines | Keep; structured steps are needed for workflows (cmd2's own `history` stays for line recall) |
| `core/workflow.py` `${var}` substitution | ~20 lines | Keep; stdlib `string.Template` would lose type-preserving whole-value substitution |
| `core/workspace.py` find-upward | ~20 lines | Keep |

## Shell and command-line parsing

Prompted by comparing with the SMAL project's cmd2-based REPL, then a throwaway spike (cmd2 4.3.0, Oct 2026).

- **cmd2 for the shell.** cmd2 4.x is built on prompt_toolkit and rich (the stack we already used), needs no
  `readline`, and is actively released. It supplies `help`, history, aliases and macros, `set`, scripts, `>` redirection,
  `|` pipes, "did you mean", and a bottom toolbar. Pinned `>=4.3,<5` because 4.x is new. `prompt_toolkit` is no longer a
  direct dependency; `rich` is `>=15` (cmd2's requirement).
- **One parser per command, from the args model** (`core/cli_parser.py`), used by `rigbox run` and the shell, so there is a
  single parsing path (replacing the hand-written `core/parse.py`). Required fields are positional, optional fields are
  `--options` (hyphenated; abbreviations are off), bools are flags, lists repeat, `Literal` fields are validated
  choices, help shows defaults. Values stay strings; pydantic converts them. Bad usage raises `UsageError` carrying the
  parser's message and exit code (2; `--help` is 0), after the parser has printed.
- **File completion is explicit**: a path argument is declared with `path_field(...)`, not guessed from its name.
- **Shell commands are generated** (`repl.build_shell`): a subclass per registry, so shells never share commands. cmd2 keys
  each command's parser by the function's `__name__` and `__qualname__`, so generated functions must set both uniquely
  or all commands silently share one parser (hit during the spike); `test_shell.py` checks every command shows its own
  help. `build_shell` refuses registry commands named like cmd2 built-ins.
- **Removed our `help` and `history` commands**: cmd2's replace them. A registry command `commands` lists commands (it
  works in one-shot runs and workflows too). Structured history still records through `Context.run`, which is what
  `workflow.save` uses. `exit` is added as an alias of cmd2's `quit`.
- Behaviour changes to know: `--option` names no longer accept underscores; a value starting with `-` needs `--opt=value`
  (negative numbers work as-is); `rigbox run help <cmd>` is now `rigbox run <cmd> --help`.
- `RigboxShell` takes an optional `stdout` (cmd2 remembers the stream it was created with, which matters for tests).
- Not verified by hand: the interactive Tab menu and bottom toolbar rendering in a Windows terminal. Completion was
  tested programmatically (`shell.complete`) and a real `rigbox.exe` session through piped stdin.

## Manifests plugin decisions

- First slice: packed structs only (no padding) with scalar types, `string`/`bytes`, numeric arrays,
  endianness, `description`/`unit`. Enums, bitfields and nested structs are deliberately deferred.
- The pydantic model comes from one code path: manifest -> generated source -> exec'd at runtime for
  validation, and the same source is what `manifest.generate` writes to disk. Exec is safe because names are
  validated identifiers and free text goes through `repr()`.
- `diff` marks type/count/length/endian/add/remove/reorder as breaking; description/unit/name as non-breaking.
- Schema errors fail the command (exit 1); `lint` findings are returned as rows (exit 0).

## Device plugin decisions

- **Transport (revised):** a `Transport` protocol (`write`, `read`, `read_chunk`, `close`) in `core/transport.py`, with
  URL scheme -> factory registered on the `Registry` (`add_transport`, `open_transport`), so any plugin can add a kind of
  device from its `rigbox_register` hook. URLs without `://` mean `serial`. pyserial is one implementation
  (`serial`, `loop`, `socket`, `rfc2217`); `replay://<capture.ndjson>` is a second, which doubles as proof that the interface
  is not pyserial-shaped. `Connection` owns the named session (name, url, options, transport). Prompted by looking at
  the SMAL project's device-agnostic protocol, then the Bose communications library's transport/session layering.
  - Factories take `(url, TransportOptions)`; options carry `timeout`, optional `write_timeout`, `baud` (serial only)
    and free-form `--param key=value` pairs, so a transport needing more settings needs no new core abstraction.
  - A finished stream raises `EOFError` instead of timing out, so `device.capture` stops and `device.expect` says
    "stream ended" rather than waiting. Communication failures raise `OSError`; read timeouts return empty bytes.
    A successful write accepts all bytes, not a silent short write or a remote acknowledgement.
  - Unexpected factory exceptions are wrapped as `CommandError("cannot open ...")`.
  - Still deferred: scripted mock devices, SWD/BLE/CAN transports (now just plugins), framing.
- **Connections:** named, held on `Context.connections`, closed by `Context.close()` (CLI `finally`, REPL exit).
  They last for a REPL session or one workflow run; `rigbox run device.connect ...` alone is pointless.
  `Connection` serializes I/O with a reentrant lock, bounds lock acquisition, assembles partial reads within one
  deadline, and translates expected I/O failures to `CommandError`. `read` may return partial packets for capture;
  `read_exactly` reports partial data and distinguishes timeout from EOF. No cross-call partial-packet buffer.
  Closing is idempotent (a failed close may be retried); Python context-manager cleanup preserves the original
  exception. Context shutdown attempts every owned resource and logs cleanup errors rather than hiding the original
  command failure. Workspace device profiles are deferred.
- **Requests:** `device.request` encodes via the existing hex/manifest path, then writes and reads one fixed-size
  response under one session lock. `--response-size` selects raw data, `--response-manifest` sizes and decodes it.
  The response timeout starts after writing; lock acquisition is bounded separately. No queue draining, automatic
  retries, response matching, or unsolicited-message routing. Separate `send` and `expect` calls remain available.
- **Write deadlines:** pyserial handles blocking I/O; serial, loop and socket writes default to the connection's
  timeout, with optional `--write-timeout`. Partial writes continue only the unsent suffix under the same deadline;
  failed writes are not replayed. No unconditional `flush()` because output draining can block without a deadline.
  RFC2217 does not support pyserial's `write_timeout`: preserve native behavior unless the user explicitly requests
  that unsupported setting, in which case fail clearly. Raw serial chunks are capped at 64 KiB; only the first byte
  waits, then available bytes are polled without extending the deadline.
- **Layering:** transports move bytes, sessions own fixed-size assembly and request coordination, existing `Codec`
  helpers own manifest serialization, and commands compose them. No additional dependency, background worker,
  transport base class, or general protocol framework. Future HID/BLE plugins must explicitly preserve/adapt native
  report identifiers and boundaries. COBS/SLIP/length-prefix framing is deferred.
- **Capture:** bounded and foreground (`--seconds`, default 10, and optional `--count`), NDJSON records
  `{ts, dir, hex, decoded | error}`. Bad or partial packets are recorded with an `error`, not fatal. No background threads.
  The same files are intended as input for state-machine trace replay.
- **Manifest reuse:** other plugins use `plugins/manifests/api.py` (`Codec`, `resolve_manifest`, `parse_values`).

## State machine plugin decisions

- **Engine:** `transitions` runs the machine; our shim passes event data as a `data` kwarg so guards can read it.
  Events are registered as `ev_<name>` so user event names can never clash with attributes transitions adds.
- **Guards/match:** `simpleeval` expressions over event data. Actions are labels reported on firing, never executed.
  A guard that can't be evaluated is an error; an event `match` that can't be evaluated (missing field) just doesn't match.
- **Property checks** (`check.py`, NetworkX, guards ignored): an explicit registry of plain check functions consumes
  shared graph indexes and returns all findings through the existing `Issue` fields. Rule identifiers and default
  severities are explicit; detection returns severity-independent findings. Loading a model does not run graph checks
  or corrections. Existing rule identifiers and severities are unchanged: unreachable-state, dead-end (unless `safe`),
  shadowed-transition, unused-event, no-safe-path (only when some state is marked `safe`).
  `MultiDiGraph` preserves parallel transitions, manifest indexes as edge keys, declaration order, and original
  transition objects; wildcard/list sources expand only in the analysis index. The manifest is not modified.
- **New structural requirements:** `State.required_events` optionally lists declared events that must have an outgoing
  transition in that state (`missing-required-event`, error). A guarded transition counts; guard coverage needs a
  separate behavioral analysis. `trapping-component` warns about reachable closed cyclic strongly connected components
  without safe states, excluding the component containing the initial state and acyclic dead ends. This detects entered
  traps, not livelock or liveness failures. Findings remain data rows, not command failures.
- **Graph dependency:** verified official `networkx` on PyPI and networkx.org: maintained by the NetworkX Developers,
  with active 3.x releases and BSD-3-Clause licensing. Require `>=3.4,<4`; pip selects a Python-compatible release
  (including Python 3.11, which the latest release no longer supports). Add no graph extras. Severity overrides,
  source locations, behavioral exploration and solver integrations remain deferred.
- **Replay:** first event whose `match` is true fires per decoded record; records with `error` are skipped. If the
  machine names a `state_field`, the packet's state is compared to the model's after the event; a mismatch is flagged
  and the model adopts the device's state (unknown state names are flagged and not adopted).
- **Command results:** plaintext includes useful details; Rich tables use the same result fields and safe text cells.
  Structured data keeps aggregate envelopes (`transitions`, `findings`, `steps`, `coverage`, or replay `rows`). Nested
  findings are serialized models; clean checks return `findings: []`. Replay includes pre-filter totals and retains
  processing errors in `--only-problems`. Finding/replay severity reflects problems, independently of exit status.
- **Coverage:** tracked inside `Simulator` (`coverage.py`): states visited, transitions fired per concrete source
  state, and both outcomes of each guard. Items are readable strings, not indexes, so coverage files survive machine
  edits (stale items are ignored). Per-run summary is logged to stderr; `--coverage-file` opts in to accumulating
  across commands (no hidden persisted state); `sm.coverage` shows the detail.
- **"Event injection" means simulated injection** (`sm.simulate` with event data). Injecting into a real device
  (an event-to-packet `send:` template) is deferred.
- **Fuzzing:** stdlib seeded random walk (`fuzz.py`), no shrinking; `hypothesis` is the upgrade path if minimal
  counterexamples become important. Event data comes from boundary values around the constants each variable is
  compared with in guards/invariants/matches, bounded by an optional `--packet` manifest. Failure = an unevaluable
  guard, or a manifest `invariants:` expression false after a step (names: `state`, `prev_state`, `event`, `fired`,
  plus the event's data). A failing fuzz run raises (non-zero exit, CI-friendly) with the seed and the shortest
  failing sequence as a `sm.simulate` command; passing runs return coverage rows. Invariants are only checked by fuzz.
- **Rendering:** Mermaid text only. Create/edit/delete commands, hierarchical states, timeouts and fuzzing deferred;
  edit the YAML directly.

## Code generation decisions

- **First slice:** `code.generate c-state-machine-header` emits a C header with declaration-order state and event enums. The template
  receives a small immutable C context built from the validated `MachineSpec`; it does not inspect the schema model
  directly. Transition execution, guard evaluation, action implementations, and other targets are
  deferred until there is a concrete need.
- **Source validation:** built-in template definitions declare an optional `source_validator`; the YAML command boundary
  runs it before context construction (the C state-machine template uses `MachineSpec.model_validate`).
  External templates receive raw YAML as `context`, without model guessing. Lookup fallback only handles unknown
  built-in names; validation and rendering errors are reported without retrying as external files.
- **Rendering and writes:** Jinja2 uses `StrictUndefined`, package-resource templates, and reusable macros. Output is
  rendered before writing, published atomically, and never overwrites an existing file without `--force`. Package data
  explicitly includes the `.j2` files.

## DSP plugin decisions

- **Signals are files** (`.wav`, `.csv` with optional `# fs=` line, `.npz`, and raw dumps via `dsp.convert` only), not
  session objects: works in one-shot runs and workflows, and arrays never go through the result contract. Commands
  return small summary rows; large data goes to `--out` CSV files.
- **Filters are YAML manifests** (`filterspec.py`), consistent with the other plugins. The manifest carries the intended
  `numeric` format, which drives export and the estimator. `cutoff` keeps scipy's per-design meaning (documented).
- **Export:** generic C/JSON/CSV plus the CMSIS-DSP layout, checked against the CMSIS-DSP docs (feedback terms added so
  a1/a2 are negated; q15 biquad stage = b0, 0, b1, b2, a1, a2; shared `postShift` with coefficients in [-1, 1);
  FIR coefficients time-reversed, q15 FIR even length >= 4). A test runs a reference CMSIS-style biquad on the exported
  values and compares it with the float filter.
- **Fixed-point gain balancing:** scipy puts almost all gain in stage 0, which made a 1 kHz/48 kHz Q15 low-pass round
  its first numerator to zero. For q15/q31 the exporter spreads gain evenly across stages (overall response unchanged),
  and reports the response error and stability of the quantized filter. Overflow headroom is not analysed.
- **Estimator** takes the user's cycles-per-MAC, overhead and clock; there are no built-in core profiles (no invented
  numbers). Direct form I biquads and a plain FIR loop are assumed; accumulator width is not counted.
- **Deferred:** signal framing/streaming from devices, noise-shaping or dithering on integer conversion, filter
  cascades/chains in one manifest, FIR design by Parks-McClellan, decimation-filter helpers, per-stage overflow analysis.

## Onboarding aids

- **Project skills** in `.claude/skills/` (committed): `onboard-me`, `add-plugin`, `add-command`. Short recipes that
  point at CLAUDE.md and the code instead of duplicating them; updated in the same change as what they describe.
- **`plugin.new`** scaffolds a workspace plugin from a template string in `plugins/plugin_template.py`. A test checks
  the generated file loads, runs, and passes the repo's ruff rules; `ty` was checked manually. Contributors adding a
  built-in plugin use the `add-plugin` skill rather than a second scaffold mode (deferred until it is needed).
- **Shell tab completion deferred:** the REPL already completes from the registry; `argcomplete` would be the package but
  its PowerShell support is the weakest and the primary environment is Windows.

## Supporting decisions

- **Result contract:** dispatch normalizes text, structured data, Rich renderables, and empty results into
  `RigboxCmdResult`. Handlers can supply multiple representations; commands reporting file creation include
  a human message and a structured `written` path.
- **Output format `auto`:** Rich on a TTY, NDJSON when piped. Rich prefers its explicit renderable, then text,
  then a structured-data table. Plaintext prefers text, then a structured-data table, then an unstyled Rich
  renderable. JSON/NDJSON never fall back to human content: human-only results raise `RenderError`, reported on
  stderr by CLI, scripts and REPL. Empty results produce `[]` or no NDJSON records without warnings.
- **Workspace file:** `rigbox.yaml` (YAML, for consistency with manifests; chosen when we targeted Python 3.10
  which has no `tomllib`, and kept after moving to 3.11). Runtime state lives in
  `.rigbox/` (history, REPL history) and should be git-ignored.
- **History:** structured JSONL steps (`run`, `args`, `ok`, `ts`), not raw text, so they replay and convert to workflows.
- **Plugin contract:** a module with a `@hookimpl rigbox_register(registry)` function. Load failures are recorded, not fatal.
- **Trust:** workspace plugins are arbitrary Python and run on startup. Only open workspaces you trust.
- **Name:** `rigbox`. It was `devbench` until an unrelated project of that name (same import name) turned up on PyPI,
  and `rigbox` was free there. Renamed names: package `rigbox`, command `rigbox`, workspace file `rigbox.yaml`,
  state directory `.rigbox/`, plugin entry-point group `rigbox.plugins`, hook `rigbox_register`, pluggy project
  `rigbox`. Workspaces created before the rename need `devbench.yaml` renamed to `rigbox.yaml` and `.devbench/` to
  `.rigbox/`. The repo's top-level folder keeps the old name `devbench`. "Free on PyPI" means no project by that exact
  name; trademark and other-registry checks were not done.
- **Python:** >= 3.11 (moved up from 3.10). Dev venv uses 3.11.
- **Lint:** `ruff` (Astral, MIT) with Google-style pydocstyle and flake8-annotations rules, to enforce the types and
  docstring standard in `CLAUDE.md`.
- **Type checker:** `ty` (Astral, beta 0.0.89, pinned exactly). Chosen over mypy: same vendor and workflow as ruff, about
  10x faster on this repo (0.6 s vs 6 s cold), and stricter; on first run it found a `Closable`/`Connection` mismatch,
  an unnarrowed `Workspace | None`, an invalid pydantic field override, and untyped dicts that a default-mode mypy run
  had passed. Risk: beta churn, mitigated by the exact pin. Fallback if that becomes painful: mypy or pyright.
  Fixes made from its findings: device commands narrow connections with `isinstance` (so `Context.connections` can stay
  a plugin-neutral `dict[str, Closable]`), `manifests/api.find_manifest` returns the path (no workspace special case in
  `manifest.generate`), `SpectrumArgs` and `InspectArgs` now share a `SignalFileArgs` base instead of overriding
  `channel` incompatibly.
