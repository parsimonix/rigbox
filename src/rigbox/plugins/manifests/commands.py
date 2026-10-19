"""manifest.* commands: show, lint, diff, generate, encode, decode."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, Registry
from rigbox.hooks import hookimpl
from rigbox.plugins.manifests.api import Codec, find_manifest, parse_values, resolve_manifest
from rigbox.plugins.manifests.build import model_source
from rigbox.plugins.manifests.check import Change, Issue, diff, lint
from rigbox.plugins.manifests.schema import StructManifest

if TYPE_CHECKING:
    from rigbox.core.context import Context


class ManifestArgs(BaseModel):
    """Arguments for commands that take one manifest."""

    manifest: str = path_field(description="Manifest file, or a name in the workspace manifests dir")


class DiffArgs(BaseModel):
    """Arguments for `manifest.diff`."""

    old: str = path_field(description="Older manifest")
    new: str = path_field(description="Newer manifest")


class GenerateArgs(ManifestArgs):
    """Arguments for `manifest.generate`."""

    out: str | None = path_field(None, description="Output .py file (default: <manifest>_model.py next to it)")


class EncodeArgs(ManifestArgs):
    """Arguments for `manifest.encode`."""

    values: str = Field(description="Field values as YAML/JSON flow mapping; bytes fields as hex strings")


class DecodeArgs(ManifestArgs):
    """Arguments for `manifest.decode`."""

    hex: str = Field(description="Packet bytes as a hex string")


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `manifest.*` commands."""

    @registry.command("manifest.show", args=ManifestArgs, record=False)
    def show(ctx: Context, args: ManifestArgs) -> list[dict[str, Any]]:
        """Show a manifest's packed layout (offset, size per field)."""
        m = resolve_manifest(ctx, args.manifest)
        return [
            {"offset": off, "size": f.size, "name": f.name, "type": f.type, "count": f.count, "length": f.length}
            for f, off in zip(m.fields, m.offsets(), strict=True)
        ]

    @registry.command("manifest.lint", args=ManifestArgs, record=False)
    def lint_(ctx: Context, args: ManifestArgs) -> list[Issue]:
        """Check a manifest: schema errors fail; style findings are returned as rows."""
        return lint(resolve_manifest(ctx, args.manifest))

    @registry.command("manifest.diff", args=DiffArgs, record=False)
    def diff_(ctx: Context, args: DiffArgs) -> list[Change]:
        """Compare two manifests and flag wire-breaking changes."""
        return diff(resolve_manifest(ctx, args.old), resolve_manifest(ctx, args.new))

    @registry.command("manifest.generate", args=GenerateArgs)
    def generate(ctx: Context, args: GenerateArgs) -> dict[str, str]:
        """Write a pydantic model source file for a manifest."""
        src = find_manifest(ctx, args.manifest)
        m = StructManifest.load_from_file(src)
        out = Path(args.out) if args.out else src.with_name(f"{src.stem}_model.py")
        out.write_text(model_source(m), encoding="utf-8")
        return {"model": m.name, "written": str(out)}

    @registry.command("manifest.encode", args=EncodeArgs, record=False)
    def encode(ctx: Context, args: EncodeArgs) -> dict[str, Any]:
        """Validate field values and pack them into bytes."""
        data = Codec(resolve_manifest(ctx, args.manifest)).encode(parse_values(args.values))
        return {"size": len(data), "hex": data.hex()}

    @registry.command("manifest.decode", args=DecodeArgs, record=False)
    def decode(ctx: Context, args: DecodeArgs) -> dict[str, Any]:
        """Unpack bytes into field values."""
        try:
            data = bytes.fromhex(args.hex.replace(" ", ""))
        except ValueError as e:
            raise CommandError(f"decode failed: {e}") from None
        return Codec(resolve_manifest(ctx, args.manifest)).decode(data)
