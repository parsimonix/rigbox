"""sm.* commands: show, check, render, simulate, coverage, fuzz, replay."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field
from rich.console import Group
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from rigbox.constants import RenderLevel
from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, Registry
from rigbox.core.render import RigboxCmdResult, create_rich_table
from rigbox.hooks import hookimpl
from rigbox.plugins.manifests.api import resolve_manifest
from rigbox.plugins.statemachine.check import check
from rigbox.plugins.statemachine.coverage import Coverage, describe, summary
from rigbox.plugins.statemachine.engine import Simulator, matches
from rigbox.plugins.statemachine.fuzz import format_steps, fuzz
from rigbox.plugins.statemachine.schema import MachineSpec

if TYPE_CHECKING:
    from rigbox.core.context import Context


class MachineArgs(BaseModel):
    """Arguments for commands that act on one machine."""

    machine: str = path_field(description="State machine file, or a name in the workspace manifests dir")


class RenderArgs(MachineArgs):
    """Arguments for `sm.render`."""

    out: str | None = path_field(None, description="Write the Mermaid text to this file instead of returning it")


class CoverageFileArgs(MachineArgs):
    """Arguments for commands that can accumulate coverage into a file."""

    coverage_file: str | None = path_field(None, description="JSON file to accumulate coverage into (merged if it exists)")


class SimulateArgs(CoverageFileArgs):
    """Arguments for `sm.simulate`."""

    steps: str = Field(description="YAML list of events: a name, or {name: {data}} to give the event data")


class ReplayArgs(CoverageFileArgs):
    """Arguments for `sm.replay`."""

    trace: str = path_field(description="NDJSON file from device.capture (path, or a name in the workspace captures dir)")
    only_problems: bool = Field(False, description="Only show diverged or unusable records")


class FuzzArgs(CoverageFileArgs):
    """Arguments for `sm.fuzz`."""

    runs: int = Field(100, ge=1, description="Number of random walks")
    length: int = Field(20, ge=1, description="Maximum events per walk")
    seed: int | None = Field(None, description="Random seed (default: a fresh one, always printed)")
    packet: str | None = path_field(None, description="Packet manifest whose field types bound the generated data")


class CoverageArgs(MachineArgs):
    """Arguments for `sm.coverage`."""

    file: str = path_field(description="Coverage JSON file written by --coverage-file")


def _resolve(path_dir: Path | None, ref: str, what: str) -> Path:
    p = Path(ref)
    if not p.is_file() and path_dir is not None:
        p = path_dir / ref if (path_dir / ref).is_file() else path_dir / f"{ref}.yaml"
    if not p.is_file():
        raise CommandError(f"{what} not found: {ref}")
    return p


def _machine(ctx: Context, ref: str) -> MachineSpec:
    return MachineSpec.load_from_file(_resolve(ctx.workspace.manifests_dir if ctx.workspace else None, ref, "state machine"))


def _finish_coverage(ctx: Context, spec: MachineSpec, cov: Coverage, path: str | None) -> Coverage:
    """Merge this run's coverage into `path` (if given), log a one-line summary, return the total."""
    if path:
        file = Path(path)
        total = Coverage.load(file) if file.is_file() else Coverage(machine=spec.name)
        total.merge(cov)
        total.save(file)
        cov = total
    ctx.log(describe(spec, cov))
    return cov


def _parse_steps(text: str) -> list[tuple[str, dict[str, Any]]]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise CommandError(f"cannot parse steps: {e}") from None
    if not isinstance(raw, list):
        raise CommandError("steps must be a list, e.g. '[start, {overheat: {temperature: 95}}]'")
    steps: list[tuple[str, dict[str, Any]]] = []
    for item in raw:
        if isinstance(item, str):
            steps.append((item, {}))
        elif isinstance(item, dict) and len(item) == 1:
            ((name, data),) = item.items()
            steps.append((str(name), data or {}))
        else:
            raise CommandError(f"bad step: {item!r}")
    return steps


def _result_table(title: str, columns: list[str], rows: list[dict[str, Any]]) -> Table:
    cells: list[list[str]] = []
    for row in rows:
        values: list[str] = []
        for column in columns:
            value = row.get(column)
            text = json.dumps(value) if isinstance(value, (dict, list)) else "" if value is None else str(value)
            values.append(escape(text))
        cells.append(values)
    table = create_rich_table(title=escape(title), columns=columns, rows=cells, show_lines=True)
    if "level" in columns:
        for rendered_row, row in zip(table.rows, rows, strict=True):
            rendered_row.style = RenderLevel(row["level"]).color
    return table


def _coverage_result(spec: MachineSpec, cov: Coverage, title: str) -> RigboxCmdResult:
    rows = summary(spec, cov)
    lines = [title]
    for row in rows:
        lines.append(f"{row['kind']}: {row['covered']}/{row['total']} ({row['percent']}%)")
        if row["uncovered"]:
            lines.append("  Uncovered: " + ", ".join(row["uncovered"]))
    return RigboxCmdResult(
        text="\n".join(lines),
        structured_data={"coverage": rows},
        rich_data=_result_table(title, ["kind", "covered", "total", "percent", "uncovered"], rows),
        level=RenderLevel.SUCCESS,
    )


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `sm.*` commands."""

    @registry.command("sm.show", args=MachineArgs, record=False)
    def show(ctx: Context, args: MachineArgs) -> RigboxCmdResult:
        """List a machine's transitions."""
        machine = _machine(ctx, args.machine)
        rows = [
            {
                "from": ",".join(machine.sources(t)) if t.source != "*" else "*",
                "event": t.event,
                "guard": t.guard,
                "to": t.target,
                "actions": ",".join(t.actions),
            }
            for t in machine.transitions
        ]
        lines = [f"{machine.name} transitions:"]
        for row in rows:
            guard = f" [{row['guard']}]" if row["guard"] else ""
            actions = f" / {row['actions']}" if row["actions"] else ""
            lines.append(f"{row['from']} --{row['event']}{guard}--> {row['to']}{actions}")
        text = "\n".join(lines) if rows else f"{machine.name}: no transitions defined"
        rich_table = _result_table(f"{machine.name} State Transition Table", ["from", "event", "guard", "to", "actions"], rows)
        return RigboxCmdResult(
            text=text,
            structured_data={"transitions": rows},
            rich_data=rich_table,
        )

    @registry.command("sm.check", args=MachineArgs, record=False)
    def check_(ctx: Context, args: MachineArgs) -> RigboxCmdResult:
        """Check structural properties and report when the machine has no findings.

        Args:
            ctx: The command execution context.
            args: The machine to check.

        Returns:
            Serialized findings and a severity-aware plaintext summary.

        Raises:
            CommandError: If the machine file cannot be found or loaded.
        """
        machine = _machine(ctx, args.machine)
        findings = check(machine)
        rows = [finding.model_dump(mode="json") for finding in findings]
        level = RenderLevel.ERROR if any(finding.level == "error" for finding in findings) else RenderLevel.WARNING if findings else RenderLevel.SUCCESS
        text = (
            "No structural issues found"
            if not findings
            else "\n".join(
                [f"Found {len(findings)} issues:"] + [f"{finding.level}: {finding.rule}: {finding.subject}: {finding.message}" for finding in findings]
            )
        )
        rich_data = (
            _result_table(f"{machine.name} Structural Check Findings", ["level", "rule", "subject", "message"], rows)
            if findings
            else Text(text, style=level.color)
        )
        return RigboxCmdResult(
            text=text,
            structured_data={"findings": rows},
            rich_data=rich_data,
            level=level,
        )

    @registry.command("sm.render", args=RenderArgs, record=False)
    def render(ctx: Context, args: RenderArgs) -> RigboxCmdResult:
        """Render a machine as a Mermaid state diagram."""
        machine = _machine(ctx, args.machine)
        mmd_text = machine.to_mermaid()
        if args.out:
            try:
                Path(args.out).write_text(mmd_text, encoding="utf-8")
            except OSError as error:
                raise CommandError(f"cannot write Mermaid diagram '{args.out}': {error}") from None
        text = f"Mermaid diagram written to: {args.out}" if args.out else mmd_text
        return RigboxCmdResult(
            text=text,
            structured_data={"written": args.out} if args.out else {"mermaid": mmd_text},
            rich_data=Text(text),
            level=RenderLevel.SUCCESS,
        )

    @registry.command("sm.simulate", args=SimulateArgs, record=False)
    def simulate(ctx: Context, args: SimulateArgs) -> RigboxCmdResult:
        """Feed events to a machine and show each step."""
        machine = _machine(ctx, args.machine)
        sim = Simulator(machine)
        events = _parse_steps(args.steps)
        rows: list[dict[str, Any]] = [{"step": i, **sim.send(name, data).model_dump()} for i, (name, data) in enumerate(events, 1)]
        _finish_coverage(ctx, machine, sim.coverage, args.coverage_file)
        lines = [f"{machine.name}: {len(rows)} simulation steps"]
        for row in rows:
            outcome = "fired" if row["fired"] else row["reason"]
            actions = " / " + ", ".join(row["actions"]) if row["actions"] else ""
            lines.append(f"{row['step']}: {row['from_state']} --{row['event']}--> {row['to_state']} ({outcome}){actions}")
        rich_table = _result_table(f"{machine.name} Simulation Steps", ["step", "event", "from_state", "to_state", "fired", "reason", "actions"], rows)
        return RigboxCmdResult(
            text="\n".join(lines),
            structured_data={"steps": rows},
            rich_data=rich_table,
            level=RenderLevel.SUCCESS,
        )

    @registry.command("sm.coverage", args=CoverageArgs, record=False)
    def coverage(ctx: Context, args: CoverageArgs) -> RigboxCmdResult:
        """Show coverage (states, transitions, guard outcomes) accumulated in a coverage file."""
        file = Path(args.file)
        if not file.is_file():
            raise CommandError(f"coverage file not found: {args.file}")
        spec = _machine(ctx, args.machine)
        cov = Coverage.load(file)
        if cov.machine != spec.name:
            raise CommandError(f"coverage file is for machine '{cov.machine}', not '{spec.name}'")
        return _coverage_result(spec, cov, f"{spec.name} Coverage Summary")

    @registry.command("sm.fuzz", args=FuzzArgs, record=False)
    def fuzz_(ctx: Context, args: FuzzArgs) -> RigboxCmdResult:
        """Random-walk the machine with boundary-value data; fails if an invariant breaks or a guard errors."""
        spec = _machine(ctx, args.machine)
        packet = resolve_manifest(ctx, args.packet) if args.packet else None
        seed = args.seed if args.seed is not None else random.randrange(2**32)
        cov = Coverage(machine=spec.name)
        failures = fuzz(spec, runs=args.runs, length=args.length, seed=seed, packet=packet, coverage=cov)
        ctx.log(f"seed {seed}: {args.runs} runs x up to {args.length} events")
        cov = _finish_coverage(ctx, spec, cov, args.coverage_file)
        if failures:
            worst = min(failures, key=lambda f: len(f.steps))  # the shortest sequence is the easiest to reproduce
            raise CommandError(
                f"fuzz found {len(failures)} failing run(s) (seed {seed}). Shortest: {worst.kind}: {worst.detail}\n"
                f'reproduce: sm.simulate {args.machine} "{format_steps(worst.steps)}"'
            )
        return _coverage_result(spec, cov, f"{spec.name}: fuzzing passed (seed {seed}, {args.runs} runs x up to {args.length} events)")

    @registry.command("sm.replay", args=ReplayArgs, record=False)
    def replay(ctx: Context, args: ReplayArgs) -> RigboxCmdResult:
        """Replay a device capture through a machine and flag where the device diverged from the model."""
        spec = _machine(ctx, args.machine)
        trace = _resolve(ctx.workspace.captures_dir if ctx.workspace else None, args.trace, "trace")
        sim = Simulator(spec)
        rows: list[dict[str, Any]] = []
        diverged, skipped = 0, 0
        lines = [line for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
        for n, line in enumerate(lines, 1):
            record = json.loads(line)
            row: dict[str, Any] = {
                "n": n,
                "ts": record.get("ts"),
                "event": None,
                "from_state": sim.state,
                "to_state": sim.state,
                "fired": False,
                "reason": "",
                "device_state": None,
                "diverged": False,
            }
            data = record.get("decoded")
            if not isinstance(data, dict):
                row["reason"] = f"skipped: {record.get('error', 'no decoded data')}"
                skipped += 1
                rows.append(row)
                continue
            event = next((e for e in spec.events if e.match and matches(e.match, data)), None)
            if event is None:
                row["reason"] = "no matching event"
            else:
                try:
                    step = sim.send(event.name, data)
                    row.update(event=event.name, to_state=step.to_state, fired=step.fired, reason=step.reason)
                except CommandError as e:
                    row.update(event=event.name, reason=f"error: {e}")
            device = data.get(spec.state_field) if spec.state_field else None
            if device is not None:
                row["device_state"] = str(device)
                if row["device_state"] not in spec.state_names:
                    row.update(diverged=True, reason=f"{row['reason']}; unknown device state".lstrip("; "))
                elif row["device_state"] != sim.state:
                    row["diverged"] = True
                    sim.set_state(row["device_state"])  # adopt the device's state so later mismatches stand alone
            diverged += row["diverged"]
            rows.append(row)
        errors = sum(row["reason"].startswith("error:") for row in rows)
        counts = f"{len(lines)} records, {diverged} diverged, {skipped} skipped, {errors} errors"
        ctx.log(counts)
        _finish_coverage(ctx, spec, sim.coverage, args.coverage_file)
        if args.only_problems:
            rows = [r for r in rows if r["diverged"] or r["reason"].startswith(("skipped", "error:"))]
        rich_table = _result_table("Replay Results", ["n", "ts", "event", "from_state", "to_state", "fired", "reason", "device_state", "diverged"], rows)
        level = RenderLevel.ERROR if errors else RenderLevel.WARNING if diverged or skipped else RenderLevel.SUCCESS
        details = [f"{spec.name} replay: {counts}"]
        for row in rows:
            details.append(
                f"{row['n']}: {row['from_state']} --{row['event'] or '(no event)'}--> {row['to_state']}; "
                f"fired={row['fired']}; device={row['device_state']}; diverged={row['diverged']}; {row['reason']}"
            )
        return RigboxCmdResult(
            text="\n".join(details),
            structured_data={"rows": rows, "records": len(lines), "diverged": diverged, "skipped": skipped, "errors": errors},
            rich_data=Group(Text(counts, style=level.color), rich_table),
            level=level,
        )
