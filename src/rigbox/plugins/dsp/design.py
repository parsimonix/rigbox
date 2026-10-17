"""Design a filter from its manifest, and analyse or apply it. IIR filters are second-order sections (SOS)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import signal as sps

from rigbox.plugins.dsp.filterspec import IIR_DESIGNS, FilterSpec

PASS_ZERO = {"lowpass": True, "bandstop": True, "highpass": False, "bandpass": False}
CORNER_DB = 3.0103  # -3 dB relative to the peak gain


@dataclass
class Design:
    """A designed filter: the spec plus SOS coefficients (IIR) or taps (FIR); exactly one is set."""

    spec: FilterSpec
    sos: NDArray[np.float64] | None = None  # (stages, 6): b0 b1 b2 1 a1 a2, conventional signs (y = b.x - a.y)
    taps: NDArray[np.float64] | None = None  # fir impulse response


def design(spec: FilterSpec) -> Design:
    """Design the filter described by `spec`."""
    wn = spec.cutoffs if len(spec.cutoffs) > 1 else spec.cutoffs[0]
    if spec.kind == "iir":
        assert spec.design is not None  # FilterSpec validation fills in the default design
        sos = sps.iirfilter(
            spec.order, wn, rp=spec.ripple_db, rs=spec.stopband_db, btype=spec.response,
            ftype=IIR_DESIGNS[spec.design], output="sos", fs=spec.fs,
        )
        return Design(spec, sos=sos)
    taps = sps.firwin(spec.taps, wn, window=spec.window, pass_zero=PASS_ZERO[spec.response], fs=spec.fs)
    return Design(spec, taps=taps)


def response(d: Design, freqs_hz: NDArray[np.float64]) -> NDArray[np.complex128]:
    """Complex frequency response at `freqs_hz`."""
    if d.sos is not None:
        return sps.sosfreqz(d.sos, worN=freqs_hz, fs=d.spec.fs)[1]
    return sps.freqz(d.taps, 1, worN=freqs_hz, fs=d.spec.fs)[1]


def pole_radii(sos: NDArray[np.float64]) -> NDArray[np.float64]:
    """Return the largest pole magnitude of each second-order section."""
    return np.array([np.max(np.abs(np.roots([1.0, s[4], s[5]]))) for s in sos])


def apply(d: Design, x: NDArray[np.float64]) -> NDArray[np.float64]:
    """Filter `x` (samples, channels) along time, starting from zero state."""
    if d.sos is not None:
        return sps.sosfilt(d.sos, x, axis=0)
    return sps.lfilter(d.taps, 1.0, x, axis=0)


def metrics(d: Design, points: int = 2048) -> dict[str, Any]:
    """Summarise the response: gains, -3 dB corners, stability, pole radius and FIR group delay."""
    freqs = np.linspace(0, d.spec.fs / 2, points)
    mag = 20 * np.log10(np.abs(response(d, freqs)) + 1e-12)
    level = mag.max() - CORNER_DB
    corners = []
    for i in np.nonzero((mag[:-1] - level) * (mag[1:] - level) < 0)[0]:  # sign change = crossing
        frac = (level - mag[i]) / (mag[i + 1] - mag[i])
        corners.append(round(float(freqs[i] + frac * (freqs[i + 1] - freqs[i])), 2))
    radius = float(pole_radii(d.sos).max()) if d.sos is not None else 0.0
    delay = (len(d.taps) - 1) / 2 if d.taps is not None and np.allclose(d.taps, d.taps[::-1]) else None
    return {
        "dc_gain_db": round(float(mag[0]), 2),
        "nyquist_gain_db": round(float(mag[-1]), 2),
        "peak_gain_db": round(float(mag.max()), 2),
        "corners_3db_hz": corners,
        "stable": radius < 1.0,
        "max_pole_radius": round(radius, 6) if d.sos is not None else None,
        "group_delay_samples": delay,
    }
