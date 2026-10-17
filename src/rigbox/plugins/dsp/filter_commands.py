"""dsp.filter.* commands: design, response, export, estimate, apply."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, Registry
from rigbox.plugins.dsp import plot
from rigbox.plugins.dsp.design import apply, design, metrics, response
from rigbox.plugins.dsp.estimate import estimate
from rigbox.plugins.dsp.export import export, to_c, to_csv, to_json
from rigbox.plugins.dsp.filterspec import find_filter, load_filter
from rigbox.plugins.dsp.signals import read_signal, write_signal

if TYPE_CHECKING:
    from rigbox.core.context import Context

EXTENSIONS = {"c": ".h", "json": ".json", "csv": ".csv"}


class FilterArgs(BaseModel):
    """Arguments naming a filter manifest."""

    filter: str = path_field(description="Filter manifest file, or a name in the workspace manifests dir")


class ResponseArgs(FilterArgs):
    """Arguments for `dsp.filter.response`."""

    points: int = Field(512, ge=8, description="Frequency points for --out / --plot")
    out: str | None = path_field(None, description="Write frequency, magnitude (dB) and phase (deg) to this CSV")
    plot: str | None = path_field(None, description="Write a magnitude/phase plot to this PNG (needs rigbox[plot])")


class ExportArgs(FilterArgs):
    """Arguments for `dsp.filter.export`."""

    format: Literal["c", "json", "csv"] = "c"
    target: Literal["generic", "cmsis"] = Field(
        "generic", description="cmsis = arm_biquad_cascade_df1 / arm_fir layout"
    )
    numeric: Literal["float32", "q15", "q31"] | None = Field(None, description="Override the manifest's number format")
    out: str | None = path_field(None, description="Output file (default: next to the manifest)")


class EstimateArgs(FilterArgs):
    """Arguments for `dsp.filter.estimate`."""

    cycles_per_mac: float = Field(1.0, gt=0, description="Cycles for one multiply-accumulate on your target")
    overhead_cycles: float = Field(0.0, ge=0, description="Extra cycles per sample (loop, load/store, ...)")
    clock_mhz: float | None = Field(None, gt=0, description="Core clock; enables load and max sample rate")


class ApplyArgs(FilterArgs):
    """Arguments for `dsp.filter.apply`."""

    src: str = path_field(description="Input signal file")
    dst: str = path_field(description="Output signal file")
    fs: float | None = Field(None, description="Sample rate if the input file doesn't carry one (csv)")
    out_dtype: Literal["int16", "int32", "float32"] | None = None


def register_filter_commands(registry: Registry) -> None:
    """Register the `dsp.filter.*` commands."""

    @registry.command("dsp.filter.design", args=FilterArgs, record=False)
    def design_(ctx: Context, args: FilterArgs) -> list[dict[str, Any]]:
        """Design a filter and list its coefficients (one row per biquad stage, or per FIR tap)."""
        d = design(load_filter(find_filter(ctx, args.filter)))
        if d.sos is not None:
            return [
                {"stage": i, "b0": s[0], "b1": s[1], "b2": s[2], "a1": s[4], "a2": s[5]} for i, s in enumerate(d.sos)
            ]
        assert d.taps is not None  # a Design has either sos or taps
        return [{"tap": i, "value": float(v)} for i, v in enumerate(d.taps)]

    @registry.command("dsp.filter.response", args=ResponseArgs, record=False)
    def response_(ctx: Context, args: ResponseArgs) -> dict[str, Any]:
        """Frequency-response summary: gains, -3 dB corners, stability, delay. --out/--plot give the full curve."""
        spec = load_filter(find_filter(ctx, args.filter))
        d = design(spec)
        if args.out or args.plot:
            freqs = np.linspace(0, spec.fs / 2, args.points)
            h = response(d, freqs)
            mag, phase = 20 * np.log10(np.abs(h) + 1e-12), np.degrees(np.unwrap(np.angle(h)))
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                np.savetxt(
                    args.out,
                    np.column_stack([freqs, mag, phase]),
                    delimiter=",",
                    fmt="%.6g",
                    header="freq_hz,magnitude_db,phase_deg",
                    comments="",
                )
            if args.plot:
                plot.plot_response(freqs, mag, phase, f"{spec.name}: {spec.design} {spec.response}", Path(args.plot))
        return metrics(d)

    @registry.command("dsp.filter.export", args=ExportArgs, record=False)
    def export_(ctx: Context, args: ExportArgs) -> dict[str, Any]:
        """Quantize coefficients and write them as a C header, JSON or CSV (generic or CMSIS-DSP layout)."""
        path = find_filter(ctx, args.filter)
        spec = load_filter(path)
        if args.numeric:
            spec = spec.model_copy(update={"numeric": args.numeric})
        d = design(spec)
        coefficients = export(d, args.target)
        renderers: dict[str, Callable[[], str]] = {
            "c": lambda: to_c(coefficients, d),
            "json": lambda: to_json(coefficients, d),
            "csv": lambda: to_csv(coefficients),
        }
        text = renderers[args.format]()
        out = Path(args.out) if args.out else path.with_suffix(EXTENSIONS[args.format])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return {
            "file": str(out), "format": args.format, "target": args.target, "numeric": spec.numeric,
            "values": len(coefficients.values), "post_shift": coefficients.post_shift,
            "max_response_error_db": coefficients.max_response_error_db, "stable": coefficients.stable,
        }

    @registry.command("dsp.filter.estimate", args=EstimateArgs, record=False)
    def estimate_(ctx: Context, args: EstimateArgs) -> dict[str, float | None]:
        """Estimate cycles, memory and CPU load of running the filter on a target you describe."""
        d = design(load_filter(find_filter(ctx, args.filter)))
        return estimate(
            d, cycles_per_mac=args.cycles_per_mac, overhead_cycles=args.overhead_cycles, clock_mhz=args.clock_mhz
        )

    @registry.command("dsp.filter.apply", args=ApplyArgs)
    def apply_(ctx: Context, args: ApplyArgs) -> dict[str, Any]:
        """Run a signal file through the designed filter (floating point, zero initial state)."""
        d = design(load_filter(find_filter(ctx, args.filter)))
        sig = read_signal(Path(args.src), fs=args.fs)
        if sig.fs != d.spec.fs:
            raise CommandError(
                f"signal is {sig.fs:g} Hz but the filter is designed for {d.spec.fs:g} Hz; resample first"
            )
        sig.samples = apply(d, sig.samples)
        clipped = write_signal(Path(args.dst), sig, dtype=args.out_dtype)
        return {"file": args.dst, "samples": sig.n, "channels": sig.channels, "clipped": clipped}
