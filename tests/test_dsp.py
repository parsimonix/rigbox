from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from rigbox.cli import main
from rigbox.core.context import Context
from rigbox.core.plugins import load_all
from rigbox.core.registry import CommandError, Registry
from rigbox.plugins.dsp.analyze import level_stats, tone_stats
from rigbox.plugins.dsp.design import apply, design, metrics
from rigbox.plugins.dsp.export import export
from rigbox.plugins.dsp.filterspec import FilterSpec, load_filter
from rigbox.plugins.dsp.signals import RawSpec, Signal, from_float, read_signal, to_float, write_signal

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy.typing as npt

LOWPASS = str(Path(__file__).parent.parent / "examples" / "lowpass_1k.yaml")


@pytest.fixture
def ctx() -> Context:
    reg = Registry()
    load_all(reg)
    return Context(registry=reg)


def spec(**overrides: object) -> FilterSpec:
    data = {"name": "F", "kind": "iir", "response": "lowpass", "fs": 48000, "cutoff": 1000, "order": 4}
    data.update(overrides)
    return FilterSpec.model_validate(data)


def gen(ctx: Context, tmp_path: Path, name: str = "s.wav", **args: object) -> Path:
    path = tmp_path / name
    ctx.run("dsp.gen", {"kind": "sine", "out": str(path), **args})
    return path


def fundamental(ctx: Context, path: Path) -> float:
    return ctx.run("dsp.inspect", {"file": str(path)})[0]["fundamental_hz"]


# --- signal files -------------------------------------------------------------------------------------------

def test_q15_mapping_and_clipping() -> None:
    assert to_float(np.array([-32768, 0, 16384], dtype=np.int16)).tolist() == [-1.0, 0.0, 0.5]
    data, clipped = from_float(np.array([-1.0, 0.5, 1.0, 1.5]), "int16")
    assert data.tolist() == [-32768, 16384, 32767, 32767] and clipped == 2


@pytest.mark.parametrize("suffix", [".wav", ".npz", ".csv"])
def test_file_roundtrip(tmp_path: Path, suffix: str) -> None:
    sig = Signal(np.column_stack([np.linspace(-0.9, 0.9, 50), np.linspace(0.5, -0.5, 50)]), 8000.0)
    write_signal(tmp_path / f"x{suffix}", sig, dtype="float32" if suffix == ".wav" else None)
    back = read_signal(tmp_path / f"x{suffix}")
    assert back.fs == 8000.0 and back.samples.shape == (50, 2)
    assert np.allclose(back.samples, sig.samples, atol=1e-6)


def test_csv_needs_a_sample_rate(tmp_path: Path) -> None:
    (tmp_path / "x.csv").write_text("0.1\n0.2\n", encoding="utf-8")
    with pytest.raises(CommandError, match="sample rate unknown"):
        read_signal(tmp_path / "x.csv")
    assert read_signal(tmp_path / "x.csv", fs=100).fs == 100


def test_raw_layouts_and_endianness(tmp_path: Path) -> None:
    sig = Signal(np.array([[0.5, -0.5], [0.25, -0.25]]), 1000.0)  # L: 0.5 0.25   R: -0.5 -0.25
    write_signal(tmp_path / "i.raw", sig)
    write_signal(tmp_path / "p.raw", sig, layout="planar")
    write_signal(tmp_path / "b.raw", sig, endian="big")
    assert np.fromfile(tmp_path / "i.raw", dtype="<i2").tolist() == [16384, -16384, 8192, -8192]
    assert np.fromfile(tmp_path / "p.raw", dtype="<i2").tolist() == [16384, 8192, -16384, -8192]
    assert np.fromfile(tmp_path / "b.raw", dtype=">i2").tolist() == [16384, -16384, 8192, -8192]
    for name, spec in (("i", RawSpec(channels=2)), ("p", RawSpec(channels=2, layout="planar")),
                       ("b", RawSpec(channels=2, endian="big"))):
        assert np.allclose(read_signal(tmp_path / f"{name}.raw", fs=1000, raw=spec).samples, sig.samples)


def test_raw_needs_explicit_conversion(tmp_path: Path, ctx: Context) -> None:
    (tmp_path / "x.raw").write_bytes(b"\x00\x00")
    with pytest.raises(CommandError, match="dsp.convert"):
        ctx.run("dsp.inspect", {"file": str(tmp_path / "x.raw")})
    with pytest.raises(CommandError, match="not a multiple"):
        read_signal(tmp_path / "x.raw", fs=1, raw=RawSpec(channels=3))


def test_unsupported_and_missing_files(tmp_path: Path, ctx: Context) -> None:
    with pytest.raises(CommandError, match="file not found"):
        ctx.run("dsp.inspect", {"file": str(tmp_path / "nope.wav")})
    (tmp_path / "x.mp3").write_bytes(b"")
    with pytest.raises(CommandError, match="unsupported file type"):
        ctx.run("dsp.inspect", {"file": str(tmp_path / "x.mp3")})
    with pytest.raises(CommandError, match="is a file name missing before '.wav'"):
        ctx.run("dsp.gen", {"kind": "sine", "out": ".wav"})  # a bare extension has no suffix, so it needs a name


# --- generators and inspection ------------------------------------------------------------------------------

def test_gen_sine_is_inspected_correctly(ctx: Context, tmp_path: Path) -> None:
    path = gen(ctx, tmp_path, freq=1000, amp=0.5)
    row = ctx.run("dsp.inspect", {"file": str(path)})[0]
    assert abs(row["fundamental_hz"] - 1000) < 1 and abs(row["peak_dbfs"] + 6.02) < 0.05
    assert abs(row["rms_dbfs"] + 9.03) < 0.05 and row["clipped"] == 0 and row["thd_db"] < -80


def test_gen_kinds_and_determinism(ctx: Context, tmp_path: Path) -> None:
    for kind in ("square", "sawtooth", "triangle", "noise", "gaussian", "sweep", "impulse", "step", "dc"):
        row = ctx.run("dsp.gen", {"kind": kind, "out": str(tmp_path / f"{kind}.npz"), "seconds": 0.1})[0]
        assert row["samples"] == 4800
        assert row["peak"] <= 1.0 or kind == "gaussian"  # gaussian has rms = amp, so peaks can exceed it
    a, b = (np.load(tmp_path / "noise.npz")["samples"] for _ in range(2))
    assert np.array_equal(a, b)  # same seed, same noise
    impulse = np.load(tmp_path / "impulse.npz")["samples"][:, 0]
    assert impulse[0] == 0.5 and not impulse[1:].any()
    step = np.load(tmp_path / "step.npz")["samples"][:, 0]
    assert step[0] == 0 and step[-1] == 0.5


def test_gen_channels_and_full_scale_clipping_is_reported(ctx: Context, tmp_path: Path) -> None:
    row = ctx.run("dsp.gen", {"kind": "dc", "out": str(tmp_path / "d.wav"), "amp": 1.0, "channels": 2})[0]
    assert row["channels"] == 2 and row["clipped"] == 2 * 48000  # +1.0 does not fit in int16


def test_tone_stats_measure_thd() -> None:
    fs, n = 48000, 48000
    t = np.arange(n) / fs
    x = 0.5 * np.sin(2 * np.pi * 997 * t) + 0.005 * np.sin(2 * np.pi * 3 * 997 * t)  # 3rd harmonic at -40 dB
    stats = tone_stats(x, fs)
    fundamental, thd, thd_n = stats["fundamental_hz"], stats["thd_db"], stats["thd_n_db"]
    assert fundamental is not None and thd is not None and thd_n is not None
    assert abs(fundamental - 997) < 0.5
    assert abs(thd + 40) < 1 and abs(thd_n + 40) < 1


def test_tone_stats_handle_short_or_silent_signals() -> None:
    assert tone_stats(np.zeros(4096), 48000)["fundamental_hz"] is None
    assert tone_stats(np.ones(10), 48000)["thd_db"] is None
    assert level_stats(np.array([1.0, -1.0, 0.0]))["clipped"] == 2


def test_spectrum_peaks_out_and_plot(ctx: Context, tmp_path: Path) -> None:
    path = gen(ctx, tmp_path, freq=1171.875, amp=0.5)  # exactly on an FFT bin for fs=48000, nfft=4096
    peaks = ctx.run("dsp.spectrum", {"file": str(path), "peaks": 1, "out": str(tmp_path / "s.csv")})
    assert abs(peaks[0]["freq_hz"] - 1171.88) < 0.1 and abs(peaks[0]["level_dbfs"] + 6.02) < 0.2
    assert (tmp_path / "s.csv").read_text(encoding="utf-8").startswith("freq_hz,level_dbfs")
    with pytest.raises(CommandError, match="does not exist"):
        ctx.run("dsp.spectrum", {"file": str(path), "channel": 3})


def test_plots_are_written_when_matplotlib_is_available(ctx: Context, tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    path = gen(ctx, tmp_path)
    ctx.run("dsp.spectrum", {"file": str(path), "plot": str(tmp_path / "spec.png")})
    ctx.run("dsp.filter.response", {"filter": LOWPASS, "plot": str(tmp_path / "resp.png")})
    assert (tmp_path / "spec.png").stat().st_size > 1000 and (tmp_path / "resp.png").stat().st_size > 1000


# --- convert and resample -----------------------------------------------------------------------------------

def test_convert_raw_to_wav_and_back(ctx: Context, tmp_path: Path) -> None:
    raw = tmp_path / "in.raw"
    np.array([1000, -1000, 2000, -2000, 3000, -3000], dtype="<i2").tofile(raw)  # 3 frames of stereo
    ctx.run("dsp.convert", {"src": str(raw), "dst": str(tmp_path / "o.wav"), "fs": 16000, "channels": 2})
    sig = read_signal(tmp_path / "o.wav")
    assert sig.fs == 16000 and sig.samples.shape == (3, 2)
    ctx.run("dsp.convert", {"src": str(tmp_path / "o.wav"), "dst": str(tmp_path / "planar.raw"), "layout": "planar"})
    assert np.fromfile(tmp_path / "planar.raw", dtype="<i2").tolist() == [1000, 2000, 3000, -1000, -2000, -3000]


def test_convert_channel_mono_and_sample_type(ctx: Context, tmp_path: Path) -> None:
    src = tmp_path / "s.npz"
    write_signal(src, Signal(np.column_stack([np.full(10, 0.5), np.full(10, -0.25)]), 8000.0))
    ctx.run("dsp.convert", {"src": str(src), "dst": str(tmp_path / "left.npz"), "channel": 0})
    assert np.allclose(read_signal(tmp_path / "left.npz").samples, 0.5)
    ctx.run("dsp.convert", {"src": str(src), "dst": str(tmp_path / "mono.raw"), "mono": True, "out_dtype": "float32"})
    assert np.allclose(np.fromfile(tmp_path / "mono.raw", dtype="<f4"), 0.125)
    with pytest.raises(CommandError, match="does not exist"):
        ctx.run("dsp.convert", {"src": str(src), "dst": str(tmp_path / "x.npz"), "channel": 2})


def test_resample_keeps_the_tone_and_changes_the_rate(ctx: Context, tmp_path: Path) -> None:
    src = gen(ctx, tmp_path, freq=1000, seconds=1.0)
    row = ctx.run("dsp.resample", {"src": str(src), "dst": str(tmp_path / "16k.wav"), "fs_out": 16000})[0]
    assert (row["ratio"], row["samples_out"]) == ("1/3", 16000)
    out = ctx.run("dsp.inspect", {"file": str(tmp_path / "16k.wav")})[0]
    assert out["fs"] == 16000 and abs(out["fundamental_hz"] - 1000) < 1 and abs(out["peak_dbfs"] + 6.02) < 0.2
    up = ctx.run(
        "dsp.resample", {"src": str(tmp_path / "16k.wav"), "dst": str(tmp_path / "44k.wav"), "fs_out": 44100}
    )[0]
    assert up["ratio"] == "441/160" and up["samples_out"] == 44100


def test_resample_rejects_fractional_rates(ctx: Context, tmp_path: Path) -> None:
    src = gen(ctx, tmp_path)
    with pytest.raises(CommandError, match="whole-number"):
        ctx.run("dsp.resample", {"src": str(src), "dst": str(tmp_path / "x.wav"), "fs_out": 22050.5})


# --- filter manifests and design ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides",
    [
        {"cutoff": [1000, 2000]},                       # lowpass takes one cutoff
        {"response": "bandpass"},                       # bandpass takes two
        {"response": "bandpass", "cutoff": [2000, 1000]},
        {"cutoff": 30000},                              # above Nyquist
        {"order": None},                                # iir needs order
        {"taps": 11},                                   # iir must not have taps
        {"design": "chebyshev1"},                       # needs ripple_db
        {"design": "chebyshev2"},                       # needs stopband_db
        {"design": "elliptic", "ripple_db": 1},         # needs stopband_db too
        {"design": "nonsense"},
        {"kind": "fir", "order": None},                 # fir needs taps
        {"kind": "fir", "order": None, "taps": 10, "response": "highpass"},  # even taps
        {"kind": "fir", "order": None, "taps": 11, "window": "nope"},
        {"numeric": "q7"},
        {"mystery": 1},
    ],
)
def test_filter_spec_rejects_bad_manifests(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec(**overrides)


def test_example_filter_loads() -> None:
    s = load_filter(Path(LOWPASS))
    assert (s.kind, s.design, s.numeric) == ("iir", "butterworth", "q15")


@pytest.mark.parametrize("extra", [
    {"design": "butterworth"}, {"design": "bessel"},
    {"design": "chebyshev1", "ripple_db": 1}, {"design": "chebyshev2", "stopband_db": 60},
    {"design": "elliptic", "ripple_db": 1, "stopband_db": 60},
])
def test_every_iir_design_is_stable_and_places_its_edge_sensibly(extra: dict[str, object]) -> None:
    m = metrics(design(spec(**extra)))
    assert m["stable"] and m["dc_gain_db"] > -3 and m["nyquist_gain_db"] < -30 and len(m["corners_3db_hz"]) == 1
    if extra["design"] == "chebyshev2":  # cutoff is the stopband edge, so the -3 dB point sits below it
        assert 100 < m["corners_3db_hz"][0] < 1000
    else:
        assert 500 < m["corners_3db_hz"][0] < 1500


def test_butterworth_corner_is_the_cutoff() -> None:
    assert abs(metrics(design(spec()))["corners_3db_hz"][0] - 1000) < 5


def test_highpass_bandpass_and_fir_group_delay() -> None:
    hp = metrics(design(spec(response="highpass")))
    assert hp["dc_gain_db"] < -60 and abs(hp["nyquist_gain_db"]) < 1
    bp = metrics(design(spec(response="bandpass", cutoff=[1000, 4000])))
    assert len(bp["corners_3db_hz"]) == 2
    fir = metrics(design(spec(kind="fir", order=None, taps=63, response="bandpass", cutoff=[1000, 4000])))
    assert fir["group_delay_samples"] == 31 and fir["max_pole_radius"] is None


def test_design_and_response_commands(ctx: Context, tmp_path: Path) -> None:
    rows = ctx.run("dsp.filter.design", {"filter": LOWPASS})
    assert len(rows) == 2 and rows[1]["b0"] == 1.0
    out = tmp_path / "resp.csv"
    (row,) = ctx.run("dsp.filter.response", {"filter": LOWPASS, "points": 64, "out": str(out)})
    assert row["stable"] and len(out.read_text(encoding="utf-8").splitlines()) == 65
    with pytest.raises(CommandError, match="not found"):
        ctx.run("dsp.filter.design", {"filter": "nope"})


def test_apply_fir_to_impulse_gives_the_taps(ctx: Context, tmp_path: Path) -> None:
    flt = tmp_path / "fir.yaml"
    manifest = {"name": "F", "kind": "fir", "response": "lowpass", "fs": 8000, "cutoff": 1000, "taps": 21}
    flt.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    ctx.run("dsp.gen", {"kind": "impulse", "out": str(tmp_path / "imp.npz"), "fs": 8000, "seconds": 0.01, "amp": 1.0})
    ctx.run(
        "dsp.filter.apply", {"filter": str(flt), "src": str(tmp_path / "imp.npz"), "dst": str(tmp_path / "out.npz")}
    )
    taps = design(load_filter(flt)).taps
    assert taps is not None
    assert np.allclose(read_signal(tmp_path / "out.npz").samples[:21, 0], taps)


def test_apply_attenuates_out_of_band_tone_and_checks_rate(ctx: Context, tmp_path: Path) -> None:
    ctx.run("dsp.gen", {"kind": "sine", "out": str(tmp_path / "hi.wav"), "freq": 10000})
    ctx.run("dsp.filter.apply", {"filter": LOWPASS, "src": str(tmp_path / "hi.wav"), "dst": str(tmp_path / "o.wav")})
    assert ctx.run("dsp.inspect", {"file": str(tmp_path / "o.wav")})[0]["rms_dbfs"] < -60
    ctx.run("dsp.gen", {"kind": "sine", "out": str(tmp_path / "x8.wav"), "fs": 8000})
    with pytest.raises(CommandError, match="resample first"):
        ctx.run(
            "dsp.filter.apply", {"filter": LOWPASS, "src": str(tmp_path / "x8.wav"), "dst": str(tmp_path / "y.wav")}
        )


# --- export -------------------------------------------------------------------------------------------------

def cmsis_biquad(
    values: Sequence[float],
    per_stage: int,
    x: npt.NDArray[np.float64],
    shift: int,
    frac: int,
) -> npt.NDArray[np.float64]:
    """Reference of arm_biquad_cascade_df1: y = b0 x + b1 x1 + b2 x2 + a1 y1 + a2 y2, then << postShift."""
    scale = 2.0**shift / 2.0**frac if frac else 1.0
    for s in range(0, len(values), per_stage):
        c = [v * scale for v in values[s : s + per_stage]]
        if per_stage == 6:
            c.pop(1)  # the q15 zero pad
        b0, b1, b2, a1, a2 = c
        x1 = x2 = y1 = y2 = 0.0
        y = np.zeros_like(x)
        for n, xn in enumerate(x):
            y[n] = b0 * xn + b1 * x1 + b2 * x2 + a1 * y1 + a2 * y2
            x2, x1, y2, y1 = x1, xn, y1, y[n]
        x = y
    return x


@pytest.mark.parametrize(
    "numeric,per_stage,frac,tolerance", [("float32", 5, 0, 1e-5), ("q31", 5, 31, 1e-6), ("q15", 6, 15, 0.02)]
)
def test_cmsis_biquad_export_reproduces_the_designed_filter(
    numeric: str, per_stage: int, frac: int, tolerance: float
) -> None:
    d = design(spec(numeric=numeric, order=4))
    c = export(d, "cmsis")
    assert c.per_stage == per_stage and c.stable
    t = np.arange(2000) / 48000
    x = 0.4 * np.sin(2 * np.pi * 300 * t) + 0.1 * np.sin(2 * np.pi * 2500 * t)
    got = cmsis_biquad(c.values, per_stage, x, c.post_shift, frac)
    assert np.max(np.abs(got - apply(d, x[:, None])[:, 0])) < tolerance


def test_generic_export_keeps_conventional_signs_and_cmsis_negates_feedback() -> None:
    d = design(spec())
    assert d.sos is not None
    generic, cmsis = export(d, "generic"), export(d, "cmsis")
    assert generic.values[3:5] == pytest.approx(d.sos[0, 4:6].astype(np.float32).tolist())
    assert cmsis.values[3:5] == pytest.approx((-d.sos[0, 4:6]).astype(np.float32).tolist())


def test_q15_cmsis_layout_and_precision_are_reported() -> None:
    c = export(design(spec(numeric="q15")), "cmsis")
    assert len(c.values) == 12 and c.values[1] == 0 and c.values[7] == 0  # zero after b0 in each stage
    assert all(-32768 <= v <= 32767 for v in c.values)
    assert c.post_shift == 1 and c.max_response_error_db < 1.0 and np.isfinite(c.max_response_error_db)


def test_fixed_point_balancing_keeps_numerators_representable() -> None:
    # without balancing, stage 0's numerator (~1.6e-5) would round to zero in Q15
    c = export(design(spec(numeric="q15")), "generic")
    assert all(v != 0 for v in (c.values[0], c.values[1], c.values[5], c.values[6]))


def test_fir_export_cmsis_is_reversed_and_q15_is_padded() -> None:
    fir = spec(kind="fir", order=None, taps=21, cutoff=1000, numeric="q15")
    generic, cmsis = export(design(fir), "generic"), export(design(fir), "cmsis")
    assert len(generic.values) == 21 and len(cmsis.values) == 22  # padded to an even count
    assert cmsis.values[1:] == generic.values[::-1] and cmsis.values[0] == 0
    f32 = export(design(spec(kind="fir", order=None, taps=21, cutoff=1000)), "cmsis")
    f32_generic = export(design(spec(kind="fir", order=None, taps=21, cutoff=1000)), "generic")
    assert len(f32.values) == 21 and f32.values == f32_generic.values[::-1]


def test_fir_taps_too_large_for_q_format_are_rejected() -> None:
    big = spec(kind="fir", order=None, taps=3, cutoff=24000 - 1, numeric="q15")
    d = design(big)
    d.taps = np.array([1.5, 0.0, 0.0])
    with pytest.raises(CommandError, match="Q format"):
        export(d, "generic")


def test_export_command_writes_c_json_and_csv(ctx: Context, tmp_path: Path) -> None:
    c_file = tmp_path / "lp.h"
    row = ctx.run("dsp.filter.export", {"filter": LOWPASS, "target": "cmsis", "out": str(c_file)})[0]
    text = c_file.read_text(encoding="utf-8")
    assert row["numeric"] == "q15" and row["stable"] and row["max_response_error_db"] < 1
    for needle in ('#include "arm_math.h"', "LOWPASS1K_NUM_STAGES 2", "LOWPASS1K_POST_SHIFT 1",
                   "static const q15_t LOWPASS1K_COEFFS[12]", "LOWPASS1K_STATE_LEN 8"):
        assert needle in text
    generic = ctx.run("dsp.filter.export", {"filter": LOWPASS, "numeric": "float32", "out": str(tmp_path / "g.h")})[0]
    assert generic["numeric"] == "float32"
    assert "static const float LOWPASS1K_COEFFS[10]" in (tmp_path / "g.h").read_text(encoding="utf-8")

    ctx.run("dsp.filter.export", {"filter": LOWPASS, "format": "json", "out": str(tmp_path / "lp.json")})
    data = json.loads((tmp_path / "lp.json").read_text(encoding="utf-8"))
    assert data["stages"] == 2 and len(data["coefficients"]) == 10 and data["numeric"] == "q15"
    ctx.run("dsp.filter.export", {"filter": LOWPASS, "format": "csv", "out": str(tmp_path / "lp.csv")})
    assert (tmp_path / "lp.csv").read_text(encoding="utf-8").splitlines()[0] == "stage,coefficients"


def test_export_default_path_is_next_to_the_manifest(ctx: Context, tmp_path: Path) -> None:
    src = tmp_path / "myfilter.yaml"
    src.write_text(Path(LOWPASS).read_text(encoding="utf-8"), encoding="utf-8")
    ctx.run("dsp.filter.export", {"filter": str(src), "format": "json"})
    assert (tmp_path / "myfilter.json").is_file()


# --- estimate -----------------------------------------------------------------------------------------------

def test_estimate_iir_and_fir(ctx: Context) -> None:
    args = {"filter": LOWPASS, "cycles_per_mac": 2, "overhead_cycles": 10, "clock_mhz": 100}
    (row,) = ctx.run("dsp.filter.estimate", args)
    assert row["macs_per_sample"] == 10 and row["cycles_per_sample"] == 30
    assert row["coeff_bytes"] == 20 and row["state_bytes"] == 16  # q15: 2 bytes per word
    assert row["load_percent"] == pytest.approx(30 * 48000 / 100e6 * 100, abs=0.01)
    assert row["max_sample_rate_hz"] == pytest.approx(100e6 / 30, abs=0.1)


def test_estimate_without_a_clock_leaves_load_blank(tmp_path: Path, ctx: Context) -> None:
    flt = tmp_path / "fir.yaml"
    flt.write_text(yaml.safe_dump({"name": "F", "kind": "fir", "response": "lowpass", "fs": 8000, "cutoff": 1000,
                                   "taps": 31, "numeric": "q31"}), encoding="utf-8")
    (row,) = ctx.run("dsp.filter.estimate", {"filter": str(flt)})
    assert (row["macs_per_sample"], row["coeff_bytes"], row["state_bytes"]) == (31, 124, 120)
    assert row["load_percent"] is None and row["max_sample_rate_hz"] is None


# --- end to end through the CLI -----------------------------------------------------------------------------

def test_cli_pipeline_gen_filter_inspect(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wav, out = tmp_path / "n.wav", tmp_path / "f.wav"
    assert main(["run", "dsp.gen", "gaussian", str(wav), "--amp", "0.3", "--seconds", "0.5"]) == 0
    assert main(["run", "dsp.filter.apply", LOWPASS, str(wav), str(out)]) == 0
    capsys.readouterr()
    assert main(["run", "--format", "json", "dsp.inspect", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["rms_dbfs"] < -20 * np.log10(3) - 10  # noise mostly removed
