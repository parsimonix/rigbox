"""Register code-generation commands for state-machine manifests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from jinja2 import TemplateError
from pydantic import BaseModel, Field, ValidationError

from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, Registry
from rigbox.core.render import RigboxCmdResult
from rigbox.hooks import hookimpl
from rigbox.plugins.code.generator import CodeGenerator
from rigbox.plugins.code.template import get_builtin_template

if TYPE_CHECKING:
    from rigbox.core.context import Context


class GenerateArgs(BaseModel):
    """Arguments for `code.generate`."""

    template: str = path_field(description="Path to the template file, or the name of a rigbox-builtin template")
    out: Path = path_field(description="Output file for the generated code")
    manifest: Path = path_field(description="Path to the manifest YAML file used to populate the template")
    force: bool = Field(False, description="Replace the output file if it already exists")


def _manifest_context(data: object) -> object:
    return data


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `code.*` commands."""

    @registry.command("code.generate", args=GenerateArgs, record=False)
    def generate(ctx: Context, args: GenerateArgs) -> RigboxCmdResult:
        """Generate code using a built-in or external Jinja template.

        Args:
            ctx: The command execution context.
            args: The template, manifest and output options.

        Returns:
            A message identifying the generated file.

        Raises:
            CommandError: If loading, validation, rendering or writing fails.
        """
        generator = CodeGenerator()
        try:
            definition = get_builtin_template(args.template)
        except CommandError as error:
            ext_templ_file = Path(args.template)
            if not ext_templ_file.is_file():
                raise CommandError(f"template '{args.template}' not found as a builtin or external file") from error
            try:
                _ext_env, jinjatempl, external_definition = generator.load_external_template(ext_templ_file, _manifest_context)
            except (OSError, ValueError, TemplateError) as error:
                raise CommandError(f"failed to load external template '{ext_templ_file}': {error}") from None
            definition = None

        try:
            manifest_data = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise CommandError(f"cannot read manifest '{args.manifest}': {error}") from None

        try:
            if definition is not None:
                try:
                    source = definition.source_validator(manifest_data) if definition.source_validator is not None else manifest_data
                except ValidationError as error:
                    problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'manifest'}: {err['msg']}" for err in error.errors())
                    raise CommandError(f"{args.manifest.name}: {problems}") from None
                generator.render_to_file(definition, source, args.out, force=args.force)
            else:
                generator.render_to_file(external_definition, manifest_data, args.out, force=args.force, template=jinjatempl)
        except OSError as error:
            raise CommandError(f"cannot write generated code '{args.out}': {error}") from None
        return RigboxCmdResult(text=f"Generated code written to '{args.out}'", structured_data={"written": str(args.out)})
