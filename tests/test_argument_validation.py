"""Verify shared dispatch rejects unknown arguments before command side effects."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from rigbox.core.context import Context
from rigbox.core.registry import CommandError, NoArgs, Registry
from rigbox.core.workflow import Step, Workflow, run_workflow
from rigbox.plugins.device.commands import RequestArgs


@pytest.mark.parametrize("mode", ["ignore", "allow", "forbid"])
def test_dispatch_rejects_extra_fields_regardless_of_model_defaults(mode: Literal["ignore", "allow", "forbid"]) -> None:
    class Args(BaseModel):
        value: int

    Args.model_config = ConfigDict(extra=mode)
    Args.model_rebuild(force=True)
    registry = Registry()
    seen: list[int] = []

    @registry.command("test.value", args=Args)
    def handler(ctx: Context, args: Args) -> None:
        seen.append(args.value)

    ctx = Context(registry)
    with pytest.raises(CommandError, match="test.value: invalid arguments: typo: Extra inputs are not permitted"):
        ctx.run("test.value", {"value": 1, "typo": 2})
    assert seen == []
    assert ctx.history.entries() == []
    ctx.run("test.value", {"value": "3"})
    assert seen == [3]


def test_workflow_reports_argument_typo_with_step_context_before_handler_runs() -> None:
    registry = Registry()
    called: list[str] = []

    @registry.command("device.request", args=RequestArgs)
    def handler(ctx: Context, args: RequestArgs) -> None:
        called.append(args.name)

    ctx = Context(registry)
    workflow = Workflow(
        name="sdk",
        steps=[
            Step(
                run="device.request",
                args={
                    "name": "dut",
                    "protocol": "race-uart",
                    "frame": "05 5a 02 00 01 03",
                    "playload_format": "ascii",
                },
            )
        ],
    )
    with pytest.raises(CommandError, match=r"workflow 'sdk' failed at step 1 .*playload_format: Extra inputs"):
        run_workflow(ctx, workflow)
    assert called == []


def test_no_argument_command_rejects_supplied_fields() -> None:
    registry = Registry()

    @registry.command("test.empty")
    def handler(ctx: Context, args: NoArgs) -> None:
        pytest.fail("invalid arguments reached handler")

    with pytest.raises(CommandError, match="unexpected: Extra inputs"):
        Context(registry).run("test.empty", {"unexpected": True})


def test_dispatch_preserves_aliases_defaults_and_constraints() -> None:
    class Args(BaseModel):
        count: int = Field(2, alias="n", ge=1)

    registry = Registry()
    seen: list[int] = []

    @registry.command("test.alias", args=Args)
    def handler(ctx: Context, args: Args) -> None:
        seen.append(args.count)

    ctx = Context(registry)
    ctx.run("test.alias")
    ctx.run("test.alias", {"n": "4"})
    with pytest.raises(CommandError, match="invalid arguments"):
        ctx.run("test.alias", {"n": 0})
    assert seen == [2, 4]
