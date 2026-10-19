"""Register code-generation commands for state-machine manifests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, Registry
from rigbox.hooks import hookimpl
from rigbox.plugins.codegen.generator import CodeGenerator
from rigbox.plugins.codegen.render import c_state_machine_context
from rigbox.plugins.codegen.template import get_builtin_template
from rigbox.plugins.statemachine.schema import MachineSpec

if TYPE_CHECKING:
    from rigbox.core.context import Context


class CStateMachineArgs(BaseModel):
    """Arguments for `codegen.state_machine`."""

    machine: str = path_field(description="State-machine YAML file")
    out: str | None = path_field(None, description="Output header (default: next to the machine file)")
    force: bool = Field(False, description="Replace the output file if it already exists")


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `codegen.*` commands."""

    @registry.command("codegen.state_machine", args=CStateMachineArgs, record=False)
    def state_machine(ctx: Context, args: CStateMachineArgs) -> dict[str, str]:
        """Generate a C header declaring a machine's states and events.

        Args:
            ctx: The command execution context.
            args: The input machine and output options.

        Returns:
            The generated header path.

        Raises:
            CommandError: If the machine is invalid, C identifiers collide, or writing fails.
        """
        source = Path(args.machine)
        spec = MachineSpec.load_from_file(source)
        path = Path("C:\\Users\\jb1039876\\Desktop\\templates\\test_template.h.j2")
        codegen = CodeGenerator()
        _env, jinjatempl, templ_def = codegen.load_external_template(path, c_state_machine_context)
        # definition = get_builtin_template("c-state-machine-header")
        output = Path(args.out) if args.out else source.with_name(f"{source.stem}_state_machine{templ_def.output_extension}")
        try:
            codegen.render_to_file(templ_def, spec, output, force=args.force, template=jinjatempl)
        except OSError as error:
            raise CommandError(f"cannot write generated header '{output}': {error}") from None
        return {"written": str(output)}
