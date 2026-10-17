"""Test signal generators."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from scipy import signal as sps

KINDS = ("sine", "square", "sawtooth", "triangle", "noise", "gaussian", "sweep", "impulse", "step", "dc")


def generate(
    kind: str, *, fs: float, n: int, freq: float, freq2: float, amp: float, seed: int, method: str = "linear"
) -> NDArray[np.float64]:
    """Return one channel of `n` samples of the signal `kind`.

    noise is uniform in [-amp, amp]; gaussian has rms = amp;
    sweep goes from freq to freq2; step is 0 for the first quarter, then amp.
    """
    t = np.arange(n) / fs
    rng = np.random.default_rng(seed)
    if kind == "sine":
        return amp * np.sin(2 * np.pi * freq * t)
    if kind == "square":
        return amp * sps.square(2 * np.pi * freq * t)
    if kind == "sawtooth":
        return amp * sps.sawtooth(2 * np.pi * freq * t)
    if kind == "triangle":
        return amp * sps.sawtooth(2 * np.pi * freq * t, width=0.5)
    if kind == "noise":
        return rng.uniform(-amp, amp, n)
    if kind == "gaussian":
        return rng.normal(0.0, amp, n)
    if kind == "sweep":
        return amp * sps.chirp(t, f0=freq, t1=t[-1] if n > 1 else 1.0, f1=freq2, method=method)
    if kind == "impulse":
        x = np.zeros(n)
        x[0] = amp
        return x
    if kind == "step":
        x = np.zeros(n)
        x[n // 4 :] = amp
        return x
    if kind == "dc":
        return np.full(n, amp)
    raise ValueError(f"unknown signal kind: {kind}")
