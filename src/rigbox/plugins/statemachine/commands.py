"""sm.* commands: show, check, render, simulate, coverage, fuzz, replay."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field

from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, Registry
from rigbox.hooks import hookimpl
from rigbox.plugins.manifests.api import resolve_manifest
from rigbox.plugins.statemachine.check import Issue, check
from rigbox.plugins.statemachine.coverage import Coverage, describe, summary
from rigbox.plugins.statemachine.engine import Simulator, matches
from rigbox.plugins.statemachine.fuzz import format_steps, fuzz
from rigbox.plugins.statemachine.render import to_mermaid
from rigbox.plugins.statemachine.schema import MachineSpec, load_machine

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

    coverage_file: str | None = path_field(
        None, description="JSON file to accumulate coverage into (merged if it exists)"
    )


class SimulateArgs(CoverageFileArgs):
    """Arguments for `sm.simulate`."""

    steps: str = Field(description="YAML list of events: a name, or {name: {data}} to give the event data")


class ReplayArgs(CoverageFileArgs):
    """Arguments for `sm.replay`."""

    trace: str = path_field(
        description="NDJSON file from device.capture (path, or a name in the workspace captures dir)"
    )
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
    return load_machine(_resolve(ctx.workspace.manifests_dir if ctx.workspace else None, ref, "state machine"))


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


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `sm.*` commands."""

    @registry.command("sm.show", args=MachineArgs, record=False)
    def show(ctx: Context, args: MachineArgs) -> list[dict[str, Any]]:
        """List a machine's transitions."""
        spec = _machine(ctx, args.machine)
        return [
            {"from": ",".join(spec.sources(t)) if t.source != "*" else "*", "event": t.event, "guard": t.guard,
             "to": t.target, "actions": ",".join(t.actions)}
            for t in spec.transitions
        ]

    @registry.command("sm.check", args=MachineArgs, record=False)
    def check_(ctx: Context, args: MachineArgs) -> list[Issue]:
        """Check structural properties: reachability, dead ends, shadowed transitions, safe paths."""
        return check(_machine(ctx, args.machine))

    @registry.command("sm.render", args=RenderArgs, record=False)
    def render(ctx: Context, args: RenderArgs) -> dict[str, str]:
        """Render a machine as a Mermaid state diagram."""
        text = to_mermaid(_machine(ctx, args.machine))
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            return {"written": args.out}
        return {"mermaid": text}

    @registry.command("sm.simulate", args=SimulateArgs, record=False)
    def simulate(ctx: Context, args: SimulateArgs) -> list[dict[str, Any]]:
        """Feed events to a machine and show each step."""
        spec = _machine(ctx, args.machine)
        sim = Simulator(spec)
        events = _parse_steps(args.steps)
        rows = [{"step": i, **sim.send(name, data).model_dump()} for i, (name, data) in enumerate(events, 1)]
        _finish_coverage(ctx, spec, sim.coverage, args.coverage_file)
        return rows

    @registry.command("sm.coverage", args=CoverageArgs, record=False)
    def coverage(ctx: Context, args: CoverageArgs) -> list[dict[str, Any]]:
        """Show coverage (states, transitions, guard outcomes) accumulated in a coverage file."""
        file = Path(args.file)
        if not file.is_file():
            raise CommandError(f"coverage file not found: {args.file}")
        spec = _machine(ctx, args.machine)
        cov = Coverage.load(file)
        if cov.machine != spec.name:
            raise CommandError(f"coverage file is for machine '{cov.machine}', not '{spec.name}'")
        return summary(spec, cov)

    @registry.command("sm.fuzz", args=FuzzArgs, record=False)
    def fuzz_(ctx: Context, args: FuzzArgs) -> list[dict[str, Any]]:
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
        return summary(spec, cov)

    @registry.command("sm.replay", args=ReplayArgs, record=False)
    def replay(ctx: Context, args: ReplayArgs) -> list[dict[str, Any]]:
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
                "n": n, "ts": record.get("ts"), "event": None, "from_state": sim.state, "to_state": sim.state,
                "fired": False, "reason": "", "device_state": None, "diverged": False,
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
        ctx.log(f"{len(lines)} records, {diverged} diverged, {skipped} skipped")
        _finish_coverage(ctx, spec, sim.coverage, args.coverage_file)
        if args.only_problems:
            rows = [r for r in rows if r["diverged"] or r["reason"].startswith("skipped")]
        return rows
