"""Mermaid stateDiagram-v2 text (renders in VS Code's markdown preview and on GitHub)."""

from __future__ import annotations

from rigbox.plugins.statemachine.schema import MachineSpec


def _escape(text: str) -> str:
    return text.replace("<", "#lt;").replace(">", "#gt;")


def to_mermaid(spec: MachineSpec) -> str:
    """Return `spec` as Mermaid stateDiagram-v2 text."""
    lines = ["stateDiagram-v2", f"    [*] --> {spec.initial}"]
    for s in spec.states:
        if s.description:
            lines.append(f"    {s.name} : {_escape(s.description)}")
    for t in spec.transitions:
        label = t.event
        if t.guard:
            label += f" [{_escape(t.guard)}]"
        if t.actions:
            label += " / " + ", ".join(t.actions)
        for src in spec.sources(t):
            lines.append(f"    {src} --> {t.target} : {label}")
    safe = [s.name for s in spec.states if s.safe]
    if safe:
        lines.append("    classDef safe fill:#d4f4d4,stroke:#2e7d32")
        lines.append(f"    class {','.join(safe)} safe")
    return "\n".join(lines) + "\n"
