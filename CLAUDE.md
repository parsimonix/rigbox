# rigbox

A manifest-driven REPL/CLI toolbox for embedded and DSP developers: device interaction, data manifests
(pydantic-backed), state machines, DSP utilities, and artifact generation.

## Commands

Run from this directory (PowerShell). The venv is `.venv`; select it as the VS Code interpreter.

```powershell
.\.venv\Scripts\python.exe -m pytest -q          # tests
.\.venv\Scripts\ruff.exe check                    # lint (annotations present, docstrings, style); add --fix for safe autofixes
.\.venv\Scripts\ty.exe check                      # type check (checks the annotations are correct); config in pyproject.toml
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"   # (re)install after dependency changes
.\.venv\Scripts\rigbox.exe                      # interactive shell (cmd2)
.\.venv\Scripts\rigbox.exe run [--format json] <command> [args]   # one-shot (options go BEFORE the command)
.\.venv\Scripts\rigbox.exe run <command> --help # a command's arguments; `run commands` lists all commands
.\.venv\Scripts\rigbox.exe script <workflow.yaml> [--var k=v]
.\.venv\Scripts\rigbox.exe init <path>          # create a workspace
```

## Architecture (decided; see `docs/decisions.md` for rationale)

- **Custom command registry** (`core/registry.py`). Each command declares a pydantic args model and returns
  `None`, a model/dict, or a list of them. REPL, CLI and workflows all dispatch through `Context.run`.
- **YAML manifests**, safe-loaded and validated with pydantic.
- **Declarative workflows**: YAML `steps` of `run` + `args`, `${var}` substitution. No Python in workflows;
  the escape hatch is a plugin command.
- **Plugins** use `pluggy`: a plugin module defines `@hookimpl def rigbox_register(registry)`
  (`from rigbox.hooks import hookimpl`). `core/plugins.py` is a thin shim that gathers modules from
  built-ins, the `rigbox.plugins` entry-point group, and `<workspace>/plugins/*.py`. A broken plugin is
  recorded (see `plugins.list`), never fatal.
- **Command-line parsing and the shell**: each command's pydantic args model becomes one argparse parser
  (`core/cli_parser.py`: required fields are positional, optional ones are `--options`, bools are flags, lists
  repeat, `Literal` fields are validated choices). `rigbox run` and the interactive shell use the same parsers, so
  they accept the same arguments and show the same `--help`. The shell (`repl.py`) is a `cmd2` app whose commands are
  generated from the registry; cmd2 supplies `help`, history, aliases, `set`, `>` and `|`. cmd2 is pinned `>=4.3,<5`.
- **Workspace**: `rigbox.yaml` marks the root; runtime state lives in `.rigbox/` (git-ignored).
- **Output**: `auto` = human on a TTY, NDJSON when piped; also `human`, `table`, `json`, `ndjson`. Human mode
  displays an explicit message when provided, otherwise a table of structured data.

Layout: `src/rigbox/core/` (framework), `src/rigbox/plugins/` (built-in plugins; domain areas go here),
`tests/`, `docs/`.

## Conventions

- Python >= 3.11 (`requires-python` in `pyproject.toml`, ruff `target-version = "py311"`). Config stays YAML for
  consistency with manifests, even though `tomllib` is now available.
- **Lint:** `ruff` is configured in `pyproject.toml` (pycodestyle, pyflakes, isort, pyupgrade, bugbear, Google-style
  pydocstyle, flake8-annotations; line length 120). New and changed code must be ruff-clean.
- **Type check:** `ty` (Astral) is pinned to an exact version in the `dev` extra because it is in beta (0.0.x) and its
  rules change between releases; upgrade it deliberately and fix what it reports. New and changed code must be
  `ty check`-clean. Fix findings in the code (narrow `Optional`s, correct the annotation) rather than suppressing them.
  Suppress only a genuine false positive, with a trailing `# ty: ignore[rule]  # reason` on the offending line. Avoid
  `assert x is not None` unless it documents a real invariant that the types cannot express.
- New feature = new plugin module with a `rigbox_register(registry)` hook; do not add domain logic to `core/`.
- Handlers may return `RigboxResponse(structured_data, HumanReadableResponse(text, type))`, a human-only response,
  or legacy data. `Context.run`, `run_tokens`, and `run_line` return a `RigboxResponse`; access `.structured_data`
  for rows and `.human_readable` for the message. Prefer returned messages over direct printing; `ctx.print(...)`
  remains available in human/table mode. Progress and diagnostics go through `ctx.log` (stderr).
- Raise `CommandError` for user-facing failures. Use `record=False` for read-only/meta commands so they stay
  out of history and saved workflows.
- Give pydantic fields a useful `description`; it feeds `<command> --help`. Required fields must include their type
  annotation and use `Field(...)`, for example `name: str = Field(..., description="Machine name")`. Fields that
  hold a file path are declared with `path_field(...)` (from `core/cli_parser.py`) instead of `Field(...)` so the
  shell tab-completes file names.
- Don't name a registry command like one of cmd2's own (`help`, `history`, `set`, `alias`, `quit`, `exit`, ...);
  `build_shell` refuses. List commands with `commands`; the shell's `help` shows them grouped by area.
- Handle Windows paths (backslashes) correctly; the dev machine is Windows 11 / PowerShell.
- **Types:** all code is fully and correctly type-annotated: every function and method parameter and return
  (including `-> None`), nested functions and closures, lambdas that are stored or passed on, class attributes, and
  module-level variables whose type isn't obvious. No bare `Any` unless the value really is untyped, and then say why.
  Annotations must be accurate, not just present: prefer precise types (`list[str]`, `Literal[...]`,
  `Callable[[Context, ArgsT], Result]`, `TypedDict`/pydantic models) over loose ones, and fix code a type checker
  would reject rather than silencing it with `# type: ignore` (if one is unavoidable, add the reason). Use
  `from __future__ import annotations`; the target is Python 3.11.
- **Command handlers** are annotated `(ctx: Context, args: TheirArgs) -> CommandResult`, or a more precise return
  type; `Row`, `CommandResult` and `Handler` live in `core/registry.py`. Open resources on `Context.connections`
  satisfy the small `Closable` protocol (anything with `close()`).
- **Docstrings:** every module, public class, public function/method and registered command handler has a PEP 257
  docstring. Module docstrings clearly and concisely explain the module's purpose and how it accomplishes it, in
  language that is easy for a first-time reader to digest. Other docstrings start with a one-line imperative summary
  ending in a period. Include a Google-style `Args:` section in every documented callable with parameters other than
  `self` or `cls`, and describe each parameter without repeating its type annotation. Include `Returns:` for every
  callable that returns a non-`None` value, describing that value. Include `Raises:` when there are relevant
  exceptions to document, and list them; omit the section when there are none. Private helpers
  need a docstring only when the name and signature don't explain them. A command handler's docstring is shown by
  `help`, so write it for the user. Keep docstrings short; explain *why* and non-obvious behaviour, not what the code
  plainly says.
- Every feature ships with tests in `tests/`. Tests cover meaningful behavior and risks; don't add tests just to meet
  a test-count or code-coverage target. Each test should make a clear, useful assertion and be concise and readable
  to a first-time reader. Run the full suite before calling work done.

## Guiding principle: simplicity and readability first

- Simplicity and readability outrank cleverness, flexibility and performance. A new contributor should be
  able to understand the repo without a heavy cognitive load.
- Prefer the obvious solution: small modules, plain functions, few layers, explicit names, minimal
  abstraction. Don't add extension points or generality before there is a second real use for them.
- If a change makes the codebase harder to explain in a few sentences, simplify it or push back before
  merging it.
- Keep docs short and current: this file, `docs/decisions.md` and a brief README should be enough to onboard.

## Keeping Claude's onboarding/dev aids current

- Treat `CLAUDE.md`, `docs/decisions.md` and any project skills/commands (`.claude/`) as part of the codebase.
  When a change alters architecture, commands, conventions or the roadmap, update them in the same change.
- Add a project skill or file only when it removes real repeated effort. Keep each one short, single-purpose and
  accurate; delete ones that go stale.
- Current project skills (`.claude/skills/<name>/SKILL.md`): `onboard-me` (new-contributor tour), `add-plugin`
  (new built-in area), `add-command` (one command). Update the relevant skill in the same change when you alter
  what it describes: command/handler conventions (`add-command`), plugin layout or registration
  (`add-plugin`), setup steps or the checks to run (`onboard-me`). `rigbox run plugin.new <name>` scaffolds a
  workspace plugin from `plugins/plugin_template.py`; keep that template passing `ruff` and `ty`.
- Don't let these files grow into manuals. If something needs pages of explanation, simplify the code instead.

## Working preferences

- Settle architectural decisions with the user before scaffolding a new area; present a recommendation,
  not an exhaustive survey.
- Keep responses concise; offer to expand rather than writing long reports.
- **Buy, don't build.** Use existing, well-validated Python packages for anything that isn't core to
  rigbox's identity (plugin management, schema/serialization, CLI/REPL plumbing, DSP math, serial/transport
  I/O, graph rendering, etc.). A thin shim layer over the package is fine for a cleaner interface or to add
  missing features; reimplementing the package's job is not. Before writing infrastructure code, check for an
  established package first.
- Still verify any new dependency before adding it: confirm it is the real, maintained, widely used package
  (exact name, maintainer, release activity, license) and that it is necessary. Record the choice and why in
  `docs/decisions.md`.
- Don't commit or push unless asked. Commit messages end with the attribution line required by the
  organization's managed settings.

## Roadmap

1. [done] Core: registry, REPL, scripted mode, renderers, workspace, history/workflows, plugin loader.
1b. [done] Plugin loading on `pluggy`, table rendering on `rich`; remaining core code reviewed and kept
   (see `docs/decisions.md`).
1c. [done] All code (src and tests) meets the types and docstring standard: `ruff check` and `ty check` are clean and
   the tests pass. ty found issues an earlier default-mode mypy run missed; two false positives are suppressed in
   `statemachine/engine.py` (the `transitions` stubs' invariant list type, and a callback that mutates `_fired`).
2. [done, first slice] Manifests plugin (`plugins/manifests/`): show, lint, diff, generate, encode, decode.
   Deferred: enums, bitfields, nested structs, interactive create/edit commands, protocol decode of streams.
3. [done] Device plugin (`plugins/device/`): ports, transports, connect/close/list, send, expect, request, capture, decoded via
   manifests. Connections use pluggable transports chosen by URL scheme (`core/transport.py`): pyserial
   (`serial`, `loop`, `socket`, `rfc2217`) and `replay://` capture playback; other plugins can register more.
  `Connection` owns deadline-bound fixed-size reads, serialized requests and idempotent/context-managed cleanup.
  Serial writes have configurable deadlines (RFC2217 excepted); shutdown attempts every resource and logs failures.
   Deferred: framing (COBS/SLIP), device profiles, background capture, SWD/BLE/CAN transport plugins, flash/DFU.
4. [done] State machines (`plugins/statemachine/`): show, check, render (Mermaid), simulate, replay of
   `device.capture` files with divergence detection, coverage tracking, seeded fuzzing with invariants.
  Structural checks use an explicit rule registry and shared NetworkX multigraph indexes; include entered closed-cycle
  warnings and per-state `required_events` declaration coverage. Guards are ignored by structural analysis.
   Deferred: injecting events into a real device, hypothesis-based shrinking, create/edit/delete commands,
   hierarchical states, checking invariants in simulate/replay.
5. [done, first slice] DSP (`plugins/dsp/`): generators, inspect/spectrum, convert/resample, filter manifests
   (design, response, apply, export to C/JSON/CSV in generic or CMSIS-DSP layout with Q15/Q31, estimate).
   Deferred: filter chains, Parks-McClellan FIR, overflow analysis, dithering, device-stream integration.
6. [done] Onboarding aids: project skills (`onboard-me`, `add-plugin`, `add-command`) and `plugin.new`.
   Deferred: shell-level tab completion for `rigbox run` (the REPL already completes; `argcomplete` would be the
   package, but PowerShell support is its weakest), a `--builtin` scaffold for repo plugins.
6b. [done] Shell on `cmd2` with parsers generated from the args models and shared with `rigbox run` (replaced the
   hand-written `core/parse.py` and prompt_toolkit loop). Open: the interactive Tab menu was tested programmatically and
   with piped input, not driven by hand in a Windows terminal.
7. [done, first slice] Code generation: `codegen.state_machine` emits C state/event enum headers through a typed Jinja
  context. Deferred: executable transitions, guards/actions, C structs/codecs, other targets, and custom templates.
  Other candidates: richer manifests (enums, bitfields, nested structs, create/edit/delete commands); device depth
  (framing, profiles, flash/DFU).

## Preference log

Add new standing preferences here as they come up (date + one line).

- 2026-10-16: Initial decisions recorded above (custom registry, YAML, declarative workflows, hybrid plugins).
- 2026-10-16: Prefer well-validated third-party packages over home-grown code (e.g. pluggy for plugins);
  thin shims on top are OK. Existing home-grown plugin loader should migrate to pluggy.
- 2026-10-16: All code must be fully and correctly type-annotated, and have standard (PEP 257 / Google-style)
  docstrings where appropriate. Existing code predates this and needs a cleanup pass (roadmap 1c).
- 2026-10-16: Simplicity and readability come first; low onboarding load. Keep CLAUDE.md, decisions doc and
  project skills short and up to date as the code evolves.
- 2026-10-16: The tool is named **rigbox** (renamed from "devbench", which is taken on PyPI). The repo's top-level
  folder is still called `devbench`; the package is `src/rigbox`, the command is `rigbox`.
