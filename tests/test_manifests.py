from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.plugins.manifests.build import build_codec, build_model, model_source
from rigbox.plugins.manifests.check import diff, lint
from rigbox.plugins.manifests.schema import StructManifest

EXAMPLE = Path(__file__).parent.parent / "examples" / "sensor_report.yaml"


def manifest(**overrides: object) -> StructManifest:
    data: dict[str, Any] = {"name": "Pkt", "fields": [{"name": "a", "type": "u16"}, {"name": "b", "type": "u8"}]}
    data.update(overrides)
    return StructManifest.model_validate(data)


@pytest.fixture
def ctx() -> Context:
    reg = Registry()
    load_all(reg)
    return Context(registry=reg)


@pytest.mark.parametrize(
    "bad_field",
    [
        {"name": "x", "type": "string"},  # string needs length
        {"name": "x", "type": "u8", "length": 2},  # length only for string/bytes
        {"name": "x", "type": "bytes", "length": 2, "count": 2},  # no arrays of bytes
        {"name": "x", "type": "bool", "count": 2},
        {"name": "class", "type": "u8"},  # python keyword
        {"name": "_x", "type": "u8"},  # not an identifier we allow
        {"name": "json", "type": "u8"},  # shadows a pydantic attribute
        {"name": "x", "type": "u128"},  # unknown type
    ],
)
def test_schema_rejects_bad_fields(bad_field: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        manifest(fields=[bad_field])


def test_schema_rejects_duplicates_and_empty() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        manifest(fields=[{"name": "a", "type": "u8"}, {"name": "a", "type": "u8"}])
    with pytest.raises(ValidationError):
        manifest(fields=[])


def test_layout_is_packed() -> None:
    m = StructManifest.load_from_file(EXAMPLE)
    assert m.offsets() == [0, 2, 6, 7, 15, 23]
    assert m.size == 25


def test_generated_model_enforces_ranges_and_lengths() -> None:
    Model = build_model(StructManifest.load_from_file(EXAMPLE))
    good = dict(id=1, temperature=1.5, ok=True, label="n", samples=[0, 0, 0, 0], blob=b"ab")
    assert Model(**good).model_dump()["id"] == 1
    bad_values = (
        {"id": 70000},
        {"samples": [1, 2]},
        {"samples": [40000, 0, 0, 0]},
        {"blob": b"a"},
        {"label": "x" * 9},
        {"extra": 1},
    )
    for bad in bad_values:
        with pytest.raises(ValidationError):
            Model(**{**good, **bad})
    assert Model.model_fields["temperature"].json_schema_extra == {"unit": "degC"}


def test_generated_source_is_valid_python() -> None:
    compile(model_source(StructManifest.load_from_file(EXAMPLE)), "generated.py", "exec")


@pytest.mark.parametrize("endian,expected", [("little", "0201"), ("big", "0102")])
def test_codec_endianness(endian: str, expected: str) -> None:
    m = manifest(endian=endian, fields=[{"name": "a", "type": "u16"}])
    assert build_codec(m).build({"a": 0x0102}).hex() == expected


def test_encode_decode_roundtrip(ctx: Context) -> None:
    values = "{id: 258, temperature: 2.5, ok: true, label: node1, samples: [1,-2,3,4], blob: dead}"
    packed = ctx.run("manifest.encode", {"manifest": str(EXAMPLE), "values": values})[0]
    assert packed["size"] == 25
    decoded = ctx.run("manifest.decode", {"manifest": str(EXAMPLE), "hex": packed["hex"]})[0]
    assert decoded == {
        "id": 258,
        "temperature": 2.5,
        "ok": True,
        "label": "node1",
        "samples": [1, -2, 3, 4],
        "blob": "dead",
    }


def test_encode_decode_errors(ctx: Context) -> None:
    with pytest.raises(CommandError, match="encode failed"):
        ctx.run("manifest.encode", {"manifest": str(EXAMPLE), "values": "{id: 70000}"})
    with pytest.raises(CommandError, match="expected 25 bytes"):
        ctx.run("manifest.decode", {"manifest": str(EXAMPLE), "hex": "00"})
    with pytest.raises(CommandError, match="not found"):
        ctx.run("manifest.show", {"manifest": "nope"})


def test_lint_rules() -> None:
    issues = lint(StructManifest.load_from_file(EXAMPLE))
    assert {(i.rule, i.field) for i in issues} == {("no-description", "blob")}
    bare = lint(manifest(fields=[{"name": "t", "type": "f32"}]))
    assert {i.rule for i in bare} == {"no-description", "no-unit"}


def test_diff_classifies_changes() -> None:
    old = manifest()
    assert diff(old, old) == []

    doc_only = manifest(description="new", fields=[{"name": "a", "type": "u16", "unit": "ms"}, {"name": "b", "type": "u8"}])
    assert [c.breaking for c in diff(old, doc_only)] == [False, False]

    widened = manifest(fields=[{"name": "a", "type": "u32"}, {"name": "b", "type": "u8"}])
    (change,) = diff(old, widened)
    assert change.breaking and change.field == "a" and "type" in change.detail

    swapped = manifest(fields=[{"name": "b", "type": "u8"}, {"name": "a", "type": "u16"}])
    assert [c.change for c in diff(old, swapped)] == ["reordered"]

    added = manifest(fields=[*old.model_dump()["fields"], {"name": "c", "type": "u8"}])
    assert [(c.change, c.field, c.breaking) for c in diff(old, added)] == [("added", "c", True)]
    assert [(c.change, c.field) for c in diff(added, old)] == [("removed", "c")]

    assert diff(old, manifest(endian="big"))[0].breaking


def test_generate_command_writes_importable_model(ctx: Context, tmp_path: Path) -> None:
    src = tmp_path / "pkt.yaml"
    src.write_text(yaml.safe_dump({"name": "Pkt", "fields": [{"name": "a", "type": "u8"}]}), encoding="utf-8")
    (row,) = ctx.run("manifest.generate", {"manifest": str(src)})
    out = Path(row["written"])
    assert out == tmp_path / "pkt_model.py"
    ns: dict[str, Any] = {}
    exec(out.read_text(encoding="utf-8"), ns)
    assert ns["Pkt"](a=3).a == 3


def test_invalid_manifest_file_reports_readable_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: Pkt\nfields: []\n", encoding="utf-8")
    with pytest.raises(CommandError, match="bad.yaml"):
        StructManifest.load_from_file(bad)
