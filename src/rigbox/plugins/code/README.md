# Code generation

`code.generate c-state-machine-header` writes a C header containing declaration-order state and event enums from a
[state machine manifest](../statemachine/README.md).

Run this example from the repository root:

```powershell
rigbox run code.generate c-state-machine-header generated\power_mode.h examples\power_mode.yaml
```

It does not yet generate transition execution, guards, or action implementations.
Existing output files are protected unless `--force` is set.

Built-in templates declare an optional source validator. The state-machine template uses
`MachineSpec.model_validate` to validate YAML before building its C context; invalid manifests report
field-level errors. The template selects the model, rather than guessing from the manifest's contents.
The renderer itself continues to accept already-validated sources.

## External templates

`code.generate` renders an external Jinja template using the YAML manifest data unchanged as `context`.
For example, a manifest containing `project: {name: sensor}` can be rendered with `{{ context.project.name }}`.
Lists, scalars and null values are also passed through without state-machine validation.

```powershell
rigbox run code.generate templates\custom.h.j2 generated\custom.h manifests\project.yaml
```