"""dsp.* commands for signal files: gen, inspect, spectrum, convert, resample (filters are in filter_commands)."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, Field
from scipy import signal as sps

from rigbox.core.cli_parser import path_field
from rigbox.core.registry import CommandError, Registry
from rigbox.hooks import hookimpl
from rigbox.plugins.dsp import analyze, plot
from rigbox.plugins.dsp.filter_commands import register_filter_commands
from rigbox.plugins.dsp.generate import generate
from rigbox.plugins.dsp.signals import RAW_SUFFIXES, RawSpec, Signal, read_signal, write_signal

if TYPE_CHECKING:
    from rigbox.core.context import Context

OutDtype = Literal["int8", "int16", "int32", "float32", "float64"]


class GenArgs(BaseModel):
    """Arguments for `dsp.gen`."""

    kind: Literal["sine", "square", "sawtooth", "triangle", "noise", "gaussian", "sweep", "impulse", "step", "dc"]
    out: str = path_field(description="Output file (.wav, .csv, .npz, .raw, .bin)")
    fs: float = Field(48000, gt=0, description="Sample rate in Hz")
    seconds: float = Field(1.0, gt=0)
    freq: float = Field(1000, gt=0, description="Frequency in Hz (sweep start)")
    freq2: float | None = Field(None, gt=0, description="Sweep end frequency (default: fs/2)")
    amp: float = Field(0.5, gt=0, le=1, description="Amplitude, 1.0 = full scale")
    channels: int = Field(1, ge=1, description="Identical copies of the signal")
    seed: int = Field(0, description="Seed for noise kinds")
    method: Literal["linear", "logarithmic"] = Field("linear", description="Sweep shape")
    out_dtype: OutDtype | None = None


class SignalFileArgs(BaseModel):
    """Arguments shared by commands that read one signal file."""

    file: str = path_field()
    fs: float | None = Field(None, description="Sample rate, for csv files without a '# fs=' line")


class InspectArgs(SignalFileArgs):
    """Arguments for `dsp.inspect`."""

    channel: int | None = Field(None, ge=0, description="Only this channel (default: all)")


class SpectrumArgs(SignalFileArgs):
    """Arguments for `dsp.spectrum`."""

    channel: int = Field(0, ge=0)
    nfft: int = Field(4096, ge=16, description="Analysis length per averaged segment")
    window: str = "hann"
    peaks: int = Field(5, ge=1, description="Number of strongest peaks to list")
    out: str | None = path_field(None, description="Write the full spectrum (freq_hz, level_dbfs) to this CSV")
    plot: str | None = path_field(None, description="Write a spectrum plot to this PNG (needs rigbox[plot])")


class ConvertArgs(BaseModel):
    """Arguments for `dsp.convert`."""

    src: str = path_field()
    dst: str = path_field()
    fs: float | None = Field(None, description="Sample rate, needed for raw dumps and csv without a '# fs=' line")
    dtype: Literal["int8", "int16", "int32", "float32", "float64"] = Field("int16", description="Raw input sample type")
    channels: int = Field(1, ge=1, description="Raw input channel count")
    layout: Literal["interleaved", "planar"] = Field("interleaved", description="Raw input/output channel layout")
    endian: Literal["little", "big"] = Field("little", description="Raw input/output byte order")
    out_dtype: OutDtype | None = Field(None, description="Output sample type (wav: int16/int32/float32)")
    channel: int | None = Field(None, ge=0, description="Keep only this channel")
    mono: bool = Field(False, description="Average all channels into one")


class ResampleArgs(BaseModel):
    """Arguments for `dsp.resample`."""

    src: str = path_field()
    dst: str = path_field()
    fs_out: float = Field(gt=0, description="Target sample rate in Hz (whole number)")
    fs: float | None = Field(None, description="Input sample rate, for csv files without a '# fs=' line")
    out_dtype: OutDtype | None = None


def _channel(sig: Signal, index: int) -> NDArray[np.float64]:
    if index >= sig.channels:
        raise CommandError(f"channel {index} does not exist (file has {sig.channels})")
    return sig.samples[:, index]


@hookimpl
def rigbox_register(registry: Registry) -> None:
    """Register the `dsp.*` commands."""

    @registry.command("dsp.gen", args=GenArgs)
    def gen(ctx: Context, args: GenArgs) -> dict[str, Any]:
        """Generate a test signal (sine, square, noise, sweep, ...) into a file."""
        n = max(1, round(args.seconds * args.fs))
        x = generate(
            args.kind,
            fs=args.fs,
            n=n,
            freq=args.freq,
            freq2=args.freq2 or args.fs / 2,
            amp=args.amp,
            seed=args.seed,
            method=args.method,
        )
        sig = Signal(np.tile(x[:, None], (1, args.channels)), args.fs)
        clipped = write_signal(Path(args.out), sig, dtype=args.out_dtype)
        return {
            "file": args.out,
            "samples": n,
            "fs": args.fs,
            "channels": args.channels,
            "peak": round(float(np.max(np.abs(x))), 6),
            "clipped": clipped,
        }

    @registry.command("dsp.inspect", args=InspectArgs, record=False)
    def inspect(ctx: Context, args: InspectArgs) -> list[dict[str, Any]]:
        """Level statistics, clipping, fundamental frequency and THD / THD+N per channel."""
        sig = read_signal(Path(args.file), fs=args.fs)
        indices = [args.channel] if args.channel is not None else range(sig.channels)
        rows: list[dict[str, Any]] = []
        for i in indices:
            x = _channel(sig, i)
            rows.append(
                {
                    "channel": i,
                    "samples": sig.n,
                    "fs": sig.fs,
                    "duration_s": round(sig.n / sig.fs, 4),
                    **analyze.level_stats(x),
                    **analyze.tone_stats(x, sig.fs),
                }
            )
        return rows

    @registry.command("dsp.spectrum", args=SpectrumArgs, record=False)
    def spectrum(ctx: Context, args: SpectrumArgs) -> list[dict[str, float]]:
        """Strongest spectral peaks (dBFS); --out writes the whole spectrum, --plot draws it."""
        sig = read_signal(Path(args.file), fs=args.fs)
        try:
            freqs, mag = analyze.spectrum(_channel(sig, args.channel), sig.fs, args.nfft, args.window)
        except ValueError as e:
            raise CommandError(f"spectrum failed: {e}") from None
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            np.savetxt(
                args.out,
                np.column_stack([freqs, mag]),
                delimiter=",",
                fmt="%.6g",
                header="freq_hz,level_dbfs",
                comments="",
            )
        if args.plot:
            plot.plot_spectrum(freqs, mag, Path(args.file).name, Path(args.plot))
        peaks = analyze.top_peaks(freqs, mag, args.peaks)
        return [{"freq_hz": round(f, 2), "level_dbfs": round(m, 2)} for f, m in peaks]

    @registry.command("dsp.convert", args=ConvertArgs)
    def convert(ctx: Context, args: ConvertArgs) -> dict[str, Any]:
        """Convert a signal file between formats, sample types, channel layouts and byte orders.

        Formats are .wav/.csv/.npz/.raw/.bin; sample types include float <-> Q15/Q31 ints.
        """
        src, dst = Path(args.src), Path(args.dst)
        raw_in = src.suffix.lower() in RAW_SUFFIXES
        raw = (
            RawSpec(dtype=args.dtype, channels=args.channels, layout=args.layout, endian=args.endian)
            if raw_in
            else None
        )
        sig = read_signal(src, fs=args.fs, raw=raw)
        if args.channel is not None:
            sig.samples = _channel(sig, args.channel)[:, None]
        elif args.mono:
            sig.samples = sig.samples.mean(axis=1, keepdims=True)
        out_dtype = args.out_dtype or (args.dtype if raw_in else None)
        clipped = write_signal(dst, sig, dtype=out_dtype, layout=args.layout, endian=args.endian)
        return {"file": args.dst, "fs": sig.fs, "channels": sig.channels, "samples": sig.n, "clipped": clipped}

    @registry.command("dsp.resample", args=ResampleArgs)
    def resample(ctx: Context, args: ResampleArgs) -> dict[str, Any]:
        """Change a signal's sample rate with a polyphase anti-aliasing filter (whole-number rates)."""
        sig = read_signal(Path(args.src), fs=args.fs)
        if sig.fs != int(sig.fs) or args.fs_out != int(args.fs_out):
            raise CommandError("resampling needs whole-number sample rates")
        ratio = Fraction(int(args.fs_out), int(sig.fs))
        out = sps.resample_poly(sig.samples, ratio.numerator, ratio.denominator, axis=0)
        result = Signal(out, float(args.fs_out))
        clipped = write_signal(Path(args.dst), result, dtype=args.out_dtype)
        return {
            "file": args.dst,
            "fs_in": sig.fs,
            "fs_out": result.fs,
            "ratio": f"{ratio.numerator}/{ratio.denominator}",
            "samples_in": sig.n,
            "samples_out": result.n,
            "clipped": clipped,
        }

    register_filter_commands(registry)
