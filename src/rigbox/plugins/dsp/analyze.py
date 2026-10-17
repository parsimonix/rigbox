"""Signal inspection: level statistics, fundamental/THD/THD+N, and spectra."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from scipy import signal as sps

CLIP_LEVEL = 0.9999  # |x| at or above this counts as clipped
MIN_SPECTRAL_SAMPLES = 256
MAIN_LOBE_BINS = 6  # half-width of the region summed around a tone (Blackman-Harris main lobe)
MAX_HARMONIC = 10


def db(x: float, floor: float = 1e-12) -> float:
    """Return the amplitude `x` in dB, floored to avoid log(0)."""
    return float(20 * np.log10(max(x, floor)))


def level_stats(x: NDArray[np.float64]) -> dict[str, float]:
    """Return peak, RMS, DC, crest factor and clipped-sample count of one channel."""
    peak, rms = float(np.max(np.abs(x))), float(np.sqrt(np.mean(x**2)))
    return {
        "peak": round(peak, 6),
        "peak_dbfs": round(db(peak), 2),
        "rms": round(rms, 6),
        "rms_dbfs": round(db(rms), 2),
        "dc": round(float(np.mean(x)), 6),
        "crest_db": round(db(peak) - db(rms), 2),
        "clipped": int(np.count_nonzero(np.abs(x) >= CLIP_LEVEL)),
    }


def tone_stats(x: NDArray[np.float64], fs: float) -> dict[str, float | None]:
    """Return fundamental frequency, THD and THD+N of a roughly single-tone signal.

    THD and THD+N are in dB relative to the fundamental. Returns Nones when the signal is too short or silent.
    """
    empty: dict[str, float | None] = {"fundamental_hz": None, "thd_db": None, "thd_n_db": None}
    if len(x) < MIN_SPECTRAL_SAMPLES or np.max(np.abs(x)) < 1e-9:
        return empty
    x = x - np.mean(x)
    power = np.abs(np.fft.rfft(x * sps.windows.blackmanharris(len(x), sym=False))) ** 2
    n_bins, bin_hz = len(power), fs / len(x)
    k0 = int(np.argmax(power[MAIN_LOBE_BINS:])) + MAIN_LOBE_BINS
    # parabolic interpolation on log power for a sub-bin frequency estimate
    a, b, c = np.log(power[k0 - 1 : k0 + 2] + 1e-300)
    offset = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    f0 = (k0 + offset) * bin_hz

    def band(center_bin: float) -> slice:
        """Return the slice of bins around `center_bin`, clipped to the spectrum."""
        c0 = int(round(center_bin))
        return slice(max(c0 - MAIN_LOBE_BINS, 0), min(c0 + MAIN_LOBE_BINS + 1, n_bins))

    fundamental = power[band(k0)].sum()
    harmonics = 0.0
    for h in range(2, MAX_HARMONIC + 1):
        if h * f0 >= fs / 2 - MAIN_LOBE_BINS * bin_hz:
            break
        harmonics += power[band(h * f0 / bin_hz)].sum()
    rest = power.sum() - power[:MAIN_LOBE_BINS].sum() - fundamental  # everything except DC and the fundamental

    def ratio(p: float) -> float:
        """Return power `p` in dB relative to the fundamental."""
        return round(float(10 * np.log10(max(p, 1e-30) / fundamental)), 2)

    return {"fundamental_hz": round(float(f0), 2), "thd_db": ratio(harmonics), "thd_n_db": ratio(rest)}


def spectrum(
    x: NDArray[np.float64], fs: float, nfft: int = 4096, window: str = "hann"
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Averaged (Welch) amplitude spectrum in dBFS: a full-scale sine reads 0 dB."""
    freqs, pxx = sps.welch(x, fs, window=window, nperseg=min(nfft, len(x)), scaling="spectrum")
    return freqs, 10 * np.log10(2 * pxx + 1e-30)


def top_peaks(
    freqs: NDArray[np.float64], mag_db: NDArray[np.float64], count: int
) -> list[tuple[float, float]]:
    """Return the `count` strongest spectral peaks as (frequency, level) pairs, strongest first."""
    idx, _ = sps.find_peaks(mag_db)
    best = sorted(idx, key=lambda i: mag_db[i], reverse=True)[:count]
    return [(float(freqs[i]), float(mag_db[i])) for i in best]
