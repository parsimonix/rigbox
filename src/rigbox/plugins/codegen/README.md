# Code generation

`codegen.state_machine` writes a C header containing declaration-order state and event enums from a
[state machine manifest](../statemachine/README.md).

Run this example from the repository root:

```powershell
rigbox run codegen.state_machine examples\power_mode.yaml   # C header with state and event enums
```

It does not yet generate transition execution, guards, or action implementations.
Existing output files are protected unless `--force` is set.