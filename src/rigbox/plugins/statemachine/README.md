# State machines

A state machine manifest lists states, events and guarded transitions (see
[`examples/power_mode.yaml`](../../../../examples/power_mode.yaml)). Guards and
event `match` expressions are safe expressions over data, such as `temperature > 90 and mode == "running"`.

Run these examples from the repository root:

```powershell
rigbox run sm.check examples\power_mode.yaml      # structural findings, including closed cycles and required events
rigbox run sm.show examples\power_mode.yaml       # transition table
rigbox run --format ndjson sm.render examples\power_mode.yaml   # Mermaid text (or --out file.mmd)
rigbox run sm.simulate examples\power_mode.yaml "[start, {overheat: {temperature: 95}}, reset]"
rigbox run sm.replay examples\power_mode.yaml captures\run1.ndjson --only-problems
```

For C state and event enum headers, see the [code generation plugin](../codegen/README.md).

`sm.check` returns a `findings` list whose entries contain `level`, `rule`, `subject`, and `message`. If there are none,
the list is empty and human output reports success (green in Rich mode). It ignores guards: a path or
declared transition is not proof that its conditions can be satisfied. Existing severities are unchanged.
`trapping-component` warns about a reachable closed cycle with no safe state, excluding the component containing
the initial state (the machine's entire operating cycle). Dead ends are reported separately.

To require an event handler in a state, add `required_events` to that state's declaration:

```yaml
- name: running
  required_events: [stop, trip]
```

Names must refer to declared events. Missing outgoing transitions produce `missing-required-event` errors;
wildcard and list sources count, as do guarded transitions. This checks declaration coverage, not guard coverage.
Events not listed remain optional. Findings are returned as data; they do not change the command's exit status.

## Coverage and fuzzing

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

`sm.replay` takes a [device capture](../device/README.md) file, turns each decoded packet into an event using the events' `match`
expressions, and reports where the device's own state (the `state_field` packet field) differs from the model.
After a divergence the model adopts the device's state, so later mismatches stand alone.

State-machine commands provide plaintext details, Rich output, and structured envelopes such as `transitions`,
`findings`, `steps`, and `coverage`. JSON returns an array containing the envelope; NDJSON emits one aggregate record,
not one line per nested item. Replay returns `rows` plus `records`, `diverged`, `skipped`, and `errors` totals before
filtering. `--only-problems` retains divergences, skipped records, and event-processing errors. Divergences and skipped
records produce warning-level results; processing errors produce error-level results without changing the exit status.