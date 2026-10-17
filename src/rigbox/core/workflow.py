"""Declarative workflows: YAML steps with `${var}` substitution."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, Field

from rigbox.core.context import Context
from rigbox.core.registry import CommandError

_VAR = re.compile(r"\$\{(\w+)\}")


class Step(BaseModel):
    """One workflow step: a command name and its args."""

    run: str
    args: dict[str, Any] = Field(default_factory=dict)


class Workflow(BaseModel):
    """A named, ordered list of steps with default variables."""

    name: str
    description: str = ""
    vars: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step]


def load_workflow(path: Path) -> Workflow:
    """Read and validate a workflow YAML file."""
    return Workflow.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def save_workflow(path: Path, wf: Workflow) -> None:
    """Write `wf` to `path` as YAML, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(wf.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def substitute(value: object, variables: dict[str, Any]) -> object:
    """Replace ``${var}`` references in strings, recursing into dicts and lists.

    A string that is exactly ``${x}`` keeps x's type; an embedded ``${x}`` is stringified.

    Raises:
        CommandError: If a referenced variable is not defined.
    """
    if isinstance(value, str):
        whole = _VAR.fullmatch(value)
        if whole:
            return _lookup(whole.group(1), variables)
        return _VAR.sub(lambda m: str(_lookup(m.group(1), variables)), value)
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


def _lookup(name: str, variables: dict[str, Any]) -> object:
    if name not in variables:
        raise CommandError(f"workflow variable not defined: {name}")
    return variables[name]


def run_workflow(
    ctx: Context, wf: Workflow, overrides: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Run steps in order, stopping at the first failure, and return the last step's rows.

    Raises:
        CommandError: If a step fails; the message names the workflow and step.
    """
    variables = {**wf.vars, **(overrides or {})}
    rows: list[dict[str, Any]] = []
    for i, step in enumerate(wf.steps, 1):
        ctx.log(f"[{i}/{len(wf.steps)}] {step.run}")
        try:
            rows = ctx.run(step.run, cast("dict[str, Any]", substitute(step.args, variables)))
        except CommandError as e:
            raise CommandError(f"workflow '{wf.name}' failed at step {i} ({step.run}): {e}") from None
    return rows
