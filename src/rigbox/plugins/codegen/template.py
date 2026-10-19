"""Describe built-in templates and the typed contexts used to render them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from rigbox.core.registry import CommandError
from rigbox.plugins.statemachine.schema import MachineSpec

from .render import CStateMachineContext, c_state_machine_context

Source = TypeVar("Source")
Context = TypeVar("Context")


@dataclass(frozen=True)
class Template(Generic[Source, Context]):
    """Describe one template and how to build its rendering context."""

    name: str
    template_path: str
    language: str
    output_extension: str
    description: str
    context_builder: Callable[[Source], Context] | None


C_STATE_MACHINE_HEADER = Template[MachineSpec, CStateMachineContext](
    name="c-state-machine-header",
    template_path="templates/c/state_machine.h.j2",
    language="c",
    output_extension=".h",
    description="Declare a machine's states and events in a C header.",
    context_builder=c_state_machine_context,
)

BUILTIN_TEMPLATES: dict[str, Template[MachineSpec, CStateMachineContext]] = {
    C_STATE_MACHINE_HEADER.name: C_STATE_MACHINE_HEADER,
}


def get_builtin_template(name: str) -> Template[MachineSpec, CStateMachineContext]:
    """Return a built-in template by name.

    Args:
        name: The registered template name.

    Returns:
        The corresponding template definition.

    Raises:
        CommandError: If no built-in template has that name.
    """
    try:
        return BUILTIN_TEMPLATES[name]
    except KeyError:
        raise CommandError(f"unknown code-generation template: {name}") from None
