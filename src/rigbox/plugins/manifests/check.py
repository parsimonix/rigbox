"""Lint rules and manifest-to-manifest diff."""

from __future__ import annotations

from pydantic import BaseModel

from rigbox.plugins.manifests.schema import StructManifest

# Changing any of these alters the bytes on the wire.
WIRE_ATTRS = ("type", "count", "length")
DOC_ATTRS = ("unit", "description")


class Issue(BaseModel):
    """One lint finding."""

    level: str  # "warning" | "info"
    rule: str
    field: str
    message: str


class Change(BaseModel):
    """One difference between two manifests."""

    change: str  # added | removed | changed | reordered
    field: str
    detail: str
    breaking: bool


def lint(m: StructManifest) -> list[Issue]:
    """Return style findings for `m` (missing descriptions and units)."""
    issues: list[Issue] = []
    if not m.description:
        issues.append(Issue(level="warning", rule="no-description", field="(struct)", message="struct has no description"))
    for f in m.fields:
        if not f.description:
            issues.append(Issue(level="warning", rule="no-description", field=f.name, message="field has no description"))
        if f.type in ("f32", "f64") and not f.unit:
            issues.append(Issue(level="info", rule="no-unit", field=f.name, message="float field has no unit"))
    return issues


def diff(old: StructManifest, new: StructManifest) -> list[Change]:
    """Return the changes from `old` to `new`, each flagged as wire-breaking or not."""
    out: list[Change] = []
    if old.endian != new.endian:
        out.append(Change(change="changed", field="(struct)", detail=f"endian: {old.endian} -> {new.endian}", breaking=True))
    for attr in ("name", "description"):
        a, b = getattr(old, attr), getattr(new, attr)
        if a != b:
            out.append(Change(change="changed", field="(struct)", detail=f"{attr}: {a!r} -> {b!r}", breaking=False))

    old_fields = {f.name: f for f in old.fields}
    new_fields = {f.name: f for f in new.fields}
    for name in old_fields.keys() - new_fields.keys():
        out.append(Change(change="removed", field=name, detail=old_fields[name].type, breaking=True))
    for name in new_fields.keys() - old_fields.keys():
        out.append(Change(change="added", field=name, detail=new_fields[name].type, breaking=True))

    shared_old = [n for n in old_fields if n in new_fields]
    shared_new = [n for n in new_fields if n in old_fields]
    if shared_old != shared_new:
        out.append(Change(change="reordered", field="(fields)", detail=f"{shared_old} -> {shared_new}", breaking=True))

    for name in shared_old:
        a, b = old_fields[name], new_fields[name]
        for attr in WIRE_ATTRS + DOC_ATTRS:
            if getattr(a, attr) != getattr(b, attr):
                out.append(
                    Change(
                        change="changed",
                        field=name,
                        detail=f"{attr}: {getattr(a, attr)!r} -> {getattr(b, attr)!r}",
                        breaking=attr in WIRE_ATTRS,
                    )
                )
    return out
