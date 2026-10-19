"""Load packaged Jinja templates, render typed contexts, and write generated files safely."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, StrictUndefined, TemplateError
from jinja2 import Template as JinjaTemplate

from rigbox.core.registry import CommandError

from .template import Context, Source, Template


class CodeGenerator:
    """Render registered built-in templates and publish their output to files."""

    def __init__(self) -> None:
        """Create a strict Jinja environment rooted at the code package."""
        self._builtin_env = Environment(
            loader=PackageLoader("rigbox.plugins.code", package_path=""),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )

    def load_builtin_template(self, definition: Template[Source, Context]) -> JinjaTemplate:
        """Load a registered template from the package resources.

        Args:
            definition: Metadata describing the packaged template.

        Returns:
            The compiled Jinja template.

        Raises:
            CommandError: If the packaged template cannot be loaded.
        """
        try:
            return self._builtin_env.get_template(definition.template_path)
        except TemplateError as error:
            raise CommandError(f"cannot load code-generation template '{definition.name}': {error}") from None

    def load_external_template(self, path: Path, context_generator: Callable[[Source], Context] | None = None) -> tuple[Environment, JinjaTemplate, Template]:
        """Load a template from an external filesystem source.

        These templates will have access to any internal templates or macros made available by Rigbox.

        Args:
            path: The filesystem path to the external template file.
            context_generator: A callable that converts a source model into the template's rendering context.

        Raises:
            ValueError: If the specified path does not exist or is not a file.

        Returns:
            tuple[Environment, JinjaTemplate, Template]: The environment, the compiled template itself, and its metadata definition.
        """
        if not path.is_file():
            raise ValueError(f"external template path does not exist or is not a file: {path}")
        ext_env = Environment(
            loader=ChoiceLoader([self._builtin_env.loader, FileSystemLoader(path.parent)]),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )
        splittext = path.name.split(".")
        name = splittext[0]
        match len(splittext):
            case 3:
                # Double extension, e.g. template.c.j2
                output_extension = "." + splittext[1]
            case 2:
                # Single extension, e.g. template.j2
                output_extension = "." + splittext[1]
            case _:
                raise ValueError(f"unexpected template filename format: {path.name}")
        definition = Template(
            name=name,
            template_path=path.parent,
            description="",
            language="unknown",
            output_extension=output_extension,
            context_builder=context_generator,
        )
        template = ext_env.get_template(path.name)
        return ext_env, template, definition

    def render(self, definition: Template[Source, Context], source: Source, template: JinjaTemplate | None = None) -> str:
        """Render `source` through a registered template's typed context builder.

        Args:
            definition: The template and its source-to-context conversion.
            source: The validated source model for this template.
            template: The compiled Jinja template to use. If None, the built-in template will be loaded.

        Returns:
            The generated source text.

        Raises:
            CommandError: If the template cannot be loaded or rendered.
        """
        tmpl = template or self.load_builtin_template(definition)
        context = definition.context_builder(source) if definition.context_builder is not None else {}
        try:
            return tmpl.render(context=context)
        except TemplateError as error:
            raise CommandError(f"cannot render code-generation template '{definition.name}': {error}") from None

    def render_to_file(
        self,
        definition: Template[Source, Context],
        source: Source,
        file_path: Path,
        force: bool = False,
        template: JinjaTemplate | None = None,
    ) -> None:
        """Render source and atomically write it to `file_path`.

        Args:
            definition: The template and its source-to-context conversion.
            source: The validated source model for this template.
            file_path: Destination for the generated source.
            force: Replace an existing destination when true.
            template: The compiled Jinja template to use. If None, the built-in template will be loaded.

        Raises:
            FileExistsError: If the destination exists and `force` is false.
            OSError: If the destination cannot be written.
            CommandError: If the template cannot be rendered.
        """
        if file_path.exists() and not force:
            raise FileExistsError(f"output already exists: {file_path}")
        output = self.render(definition, source, template=template)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=file_path.parent, prefix=f".{file_path.name}.", suffix=".tmp", delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(output)
            if force:
                os.replace(temporary_path, file_path)
            else:
                os.link(temporary_path, file_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
