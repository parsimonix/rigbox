"""What other plugins use from manifests: find a manifest, then encode/decode packets with it."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import construct
import yaml
from pydantic import ValidationError

from rigbox.core.registry import CommandError
from rigbox.plugins.manifests.build import build_codec, build_model
from rigbox.plugins.manifests.schema import StructManifest, load_manifest

if TYPE_CHECKING:
    from rigbox.core.context import Context


def find_manifest(ctx: Context, ref: str) -> Path:
    """Return the path of a manifest given a file path, or a name in the workspace manifests dir.

    Raises:
        CommandError: If no such manifest exists.
    """
    path = Path(ref)
    if not path.is_file() and ctx.workspace is not None:
        path = ctx.workspace.find_file(ctx.workspace.manifests_dir, ref) or path
    if not path.is_file():
        raise CommandError(f"manifest not found: {ref}")
    return path


def resolve_manifest(ctx: Context, ref: str) -> StructManifest:
    """Load a manifest from a file path, or by name from the workspace manifests dir."""
    return load_manifest(find_manifest(ctx, ref))


def parse_values(text: str) -> dict[str, Any]:
    """Parse `--values`: a YAML/JSON flow mapping such as '{id: 1, ok: true}'."""
    try:
        values = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise CommandError(f"cannot parse values: {e}") from None
    if not isinstance(values, dict):
        raise CommandError("values must be a mapping, e.g. '{id: 1, ok: true}'")
    return values


class Codec:
    """Packs and unpacks one manifest's packets. Build once, reuse for many packets."""

    def __init__(self, manifest: StructManifest) -> None:
        """Build the pydantic model and `construct` struct for `manifest`."""
        self.manifest = manifest
        self._model = build_model(manifest)
        self._struct = build_codec(manifest)
        self._bytes_fields = [f.name for f in manifest.fields if f.type == "bytes"]

    @property
    def size(self) -> int:
        """Packet size in bytes."""
        return self.manifest.size

    def encode(self, values: dict[str, Any]) -> bytes:
        """Validate `values` and pack them into a packet; bytes fields may be hex strings.

        Raises:
            CommandError: If validation or packing fails.
        """
        values = dict(values)
        try:
            for name in self._bytes_fields:  # bytes fields are given as hex strings
                if isinstance(values.get(name), str):
                    values[name] = bytes.fromhex(values[name])
            return self._struct.build(self._model(**values).model_dump())
        except (ValidationError, ValueError, construct.ConstructError) as e:
            raise CommandError(f"encode failed: {e}") from None

    def decode(self, data: bytes) -> dict[str, Any]:
        """Unpack one packet into field values; bytes fields are returned as hex strings.

        Raises:
            CommandError: If `data` is the wrong size or does not validate.
        """
        if len(data) != self.size:
            raise CommandError(f"expected {self.size} bytes, got {len(data)}")
        try:
            parsed = self._struct.parse(data)
            model = self._model(**{f.name: parsed[f.name] for f in self.manifest.fields})
        except (ValidationError, ValueError, construct.ConstructError) as e:
            raise CommandError(f"decode failed: {e}") from None
        values = model.model_dump()
        for name in self._bytes_fields:
            values[name] = values[name].hex()
        return values
