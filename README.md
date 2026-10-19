# rigbox

A manifest-driven REPL and CLI toolbox for embedded and DSP developers.

## Setup

Requires Python 3.11 or newer.

```shell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check    # lint
.\.venv\Scripts\ty.exe check      # type check
```

## Use

```shell
rigbox init my-project        # create a workspace (rigbox.yaml)
rigbox                        # interactive shell (run from inside a workspace)
rigbox run commands           # list the commands
rigbox run commands --help    # one command's arguments
rigbox run --format json commands
rigbox script workflows\x.yaml --var key=value
```

Output is a human message (or a table for data-only commands) on a terminal and NDJSON when piped.
Select `--format human`, `table`, `json`, or `ndjson` explicitly when needed.

### The shell

The shell is a [cmd2](https://github.com/python-cmd2/cmd2) app; every rigbox command is a shell command with the same
arguments and `--help` as `rigbox run`. Tab completes command names, option names, choices and file names. cmd2's own
commands work too:

```shell
rigbox> help                           # commands grouped by area; `help commands` or `commands --help` for one
rigbox> set format json                # print results as JSON (human, table, json, ndjson, auto)
rigbox> alias create list commands     # then: list
rigbox> commands | findstr workspace   # pipe to a shell command
rigbox> commands > commands.txt        # redirect to a file
rigbox> history                        # what you typed (kept in .rigbox/ inside a workspace)
rigbox> exit                           # or quit, or Ctrl-D
```

The bar under the prompt shows the workspace, open device connections and output format.

## Plugins

See each plugin's README for commands, examples and behavior:

- [Manifests](src/rigbox/plugins/manifests/README.md)
- [Devices and transports](src/rigbox/plugins/device/README.md)
- [State machines](src/rigbox/plugins/statemachine/README.md)
- [Code generation](src/rigbox/plugins/code/README.md)
- [DSP](src/rigbox/plugins/dsp/README.md)
- [Built-in utilities and adding plugins](src/rigbox/plugins/README.md)

## Layout

- `src/rigbox/core/`: framework (registry, parsing, rendering, workspace, workflows, plugin loading)
- `src/rigbox/plugins/`: built-in plugins
- `docs/decisions.md`: architecture decisions and rationale
- `CLAUDE.md`: conventions and preferences for contributors and Claude Code
