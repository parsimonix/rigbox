# Manifests

A manifest is a YAML description of a packed binary packet (see
[`examples/sensor_report.yaml`](../../../../examples/sensor_report.yaml)). Field types:
`u8..u64`, `i8..i64`, `f32`, `f64`, `bool`, `string`/`bytes` (need `length`), and `count` for numeric arrays.

Run these examples from the repository root:

```powershell
rigbox run manifest.show examples\sensor_report.yaml     # packed layout: offset and size per field
rigbox run manifest.lint examples\sensor_report.yaml     # style findings
rigbox run manifest.diff old.yaml new.yaml               # flags wire-breaking changes
rigbox run manifest.generate examples\sensor_report.yaml # writes sensor_report_model.py (pydantic)
rigbox run manifest.encode examples\sensor_report.yaml "{id: 1, temperature: 2.5, ok: true, label: n1, samples: [1,2,3,4], blob: dead}"
rigbox run manifest.decode examples\sensor_report.yaml <hex>
```

Inside a workspace, manifests can be referenced by name from `manifests/`.