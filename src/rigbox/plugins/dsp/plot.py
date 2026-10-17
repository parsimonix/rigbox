"""PNG plots via matplotlib, which is an optional extra (pip install "rigbox[plot]")."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import numpy as np
from numpy.typing import NDArray

from rigbox.core.registry import CommandError


def _pyplot() -> ModuleType:
    try:
        import matplotlib

        matplotlib.use("Agg")  # file output only; never opens a window
        import matplotlib.pyplot as plt
    except ImportError:
        raise CommandError('plotting needs matplotlib: pip install "rigbox[plot]"') from None
    return plt


def plot_response(
    freqs: NDArray[np.float64],
    mag_db: NDArray[np.float64],
    phase_deg: NDArray[np.float64],
    title: str,
    path: Path,
) -> None:
    """Save a magnitude and phase plot of a frequency response to the PNG `path`."""
    plt = _pyplot()
    fig, (top, bottom) = plt.subplots(2, 1, sharex=True, figsize=(8, 6))
    top.plot(freqs, mag_db)
    top.set_ylabel("Magnitude (dB)")
    top.set_ylim(max(mag_db.min(), -120), mag_db.max() + 5)
    bottom.plot(freqs, phase_deg)
    bottom.set_ylabel("Phase (deg)")
    bottom.set_xlabel("Frequency (Hz)")
    top.set_title(title)
    for ax in (top, bottom):
        ax.grid(True, alpha=0.3)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def plot_spectrum(freqs: NDArray[np.float64], mag_db: NDArray[np.float64], title: str, path: Path) -> None:
    """Save a spectrum plot (level in dBFS against frequency) to the PNG `path`."""
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(freqs, mag_db)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Level (dBFS)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
