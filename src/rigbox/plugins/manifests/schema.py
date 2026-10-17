"""The manifest format: a packed binary struct described in YAML, validated by pydantic."""

from __future__ import annotations

import keyword
import re
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rigbox.core.registry import CommandError

# (min, max) for each integer type
INT_RANGES = {
    f"{kind}{bits}": (0, 2**bits - 1) if kind == "u" else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
    for kind in "ui"
    for bits in (8, 16, 32, 64)
}
SCALAR_SIZES = {**{t: int(t[1:]) // 8 for t in INT_RANGES}, "f32": 4, "f64": 8, "bool": 1}

_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _check_identifier(value: str, *, is_field: bool) -> str:
    """Return `value` if it is a usable Python identifier (and, for fields, not a pydantic attribute)."""
    if not _IDENT.fullmatch(value) or keyword.iskeyword(value):
        raise ValueError(f"'{value}' is not a usable Python identifier")
    if is_field and (hasattr(BaseModel, value) or value.startswith("model_")):
        raise ValueError(f"'{value}' clashes with a pydantic attribute")
    return value


class FieldSpec(BaseModel):
    """One field of a packed struct."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal[
        "u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "f32", "f64", "bool", "string", "bytes"
    ]
    length: int | None = Field(None, ge=1, description="Byte length; string and bytes only")
    count: int | None = Field(None, ge=1, description="Array length; numeric types only")
    description: str = ""
    unit: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_identifier(v, is_field=True)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.type in ("string", "bytes"):
            if self.length is None:
                raise ValueError(f"type '{self.type}' needs 'length'")
            if self.count is not None:
                raise ValueError(f"'count' is not allowed for type '{self.type}'")
        else:
            if self.length is not None:
                raise ValueError("'length' is only for string and bytes")
            if self.count is not None and self.type == "bool":
                raise ValueError("'count' is not allowed for type 'bool'")
        return self

    @property
    def size(self) -> int:
        """Size in bytes, including all array elements."""
        if self.length is not None:
            return self.length
        return SCALAR_SIZES[self.type] * (self.count or 1)


class StructManifest(BaseModel):
    """A packed binary struct: its name, byte order and ordered fields."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    endian: Literal["little", "big"] = "little"
    fields: list[FieldSpec] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_identifier(v, is_field=False)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [f.name for f in self.fields]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate field names: {', '.join(dupes)}")
        return self

    def offsets(self) -> list[int]:
        """Byte offset of each field (the layout is packed, with no padding)."""
        out: list[int] = []
        pos = 0
        for f in self.fields:
            out.append(pos)
            pos += f.size
        return out

    @property
    def size(self) -> int:
        """Total packed size in bytes."""
        return sum(f.size for f in self.fields)


def load_manifest(path: Path) -> StructManifest:
    """Read and validate the manifest at `path`.

    Raises:
        CommandError: If the file cannot be read or does not validate.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise CommandError(f"cannot read {path}: {e}") from None
    try:
        return StructManifest.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'manifest'}: {err['msg']}" for err in e.errors())
        raise CommandError(f"{path.name}: {problems}") from None
