"""Read and write signal files.

In memory a signal is float64 with shape (samples, channels), full scale = +-1.0.
Integer file samples map to float as value / 2**(bits-1), so int16 is Q15 and int32 is Q31.
Formats: .wav, .csv (optional `# fs=...` first line), .npz (samples + fs), and headerless .raw/.bin dumps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, Field
from scipy.io import wavfile

from rigbox.core.registry import CommandError

RAW_SUFFIXES = {".raw", ".bin"}
INT_BITS = {"int8": 8, "int16": 16, "int32": 32}


@dataclass
class Signal:
    """Multichannel samples plus their sample rate."""

    samples: NDArray[np.float64]  # (n, channels) float64
    fs: float

    @property
    def n(self) -> int:
        """Number of samples per channel."""
        return self.samples.shape[0]

    @property
    def channels(self) -> int:
        """Number of channels."""
        return self.samples.shape[1]


class RawSpec(BaseModel):
    """How a headerless dump is laid out."""

    dtype: Literal["int8", "int16", "int32", "float32", "float64"] = "int16"
    channels: int = Field(1, ge=1)
    layout: Literal["interleaved", "planar"] = "interleaved"
    endian: Literal["little", "big"] = "little"


def _unsupported(path: Path) -> CommandError:
    """Build the error for a file whose extension isn't a signal format, hinting at a missing file name."""
    hint = f" (is a file name missing before '{path.name}'?)" if path.name.startswith(".") and not path.suffix else ""
    return CommandError(f"unsupported file type '{path.suffix}'{hint}: use .wav, .csv, .npz, .raw or .bin")


def to_float(a: NDArray[np.generic]) -> NDArray[np.float64]:
    """Convert file samples of any dtype to float64 with full scale = +-1.0."""
    if a.dtype == np.uint8:  # 8-bit WAV is unsigned
        return (a.astype(np.float64) - 128) / 128
    if a.dtype.kind == "i":
        return a.astype(np.float64) / 2 ** (a.dtype.itemsize * 8 - 1)
    return a.astype(np.float64)


def from_float(x: NDArray[np.float64], dtype: str) -> tuple[NDArray[np.generic], int]:
    """Convert to `dtype`, clipping integers to range. Returns (array, number of clipped samples)."""
    if dtype.startswith("float"):
        return x.astype(dtype), 0
    bits = INT_BITS[dtype]
    lo, hi = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    scaled = np.rint(x * 2 ** (bits - 1))
    clipped = int(np.count_nonzero((scaled > hi) | (scaled < lo)))
    return np.clip(scaled, lo, hi).astype(dtype), clipped


def read_signal(path: Path, *, fs: float | None = None, raw: RawSpec | None = None) -> Signal:
    """Read a signal file; `fs` and `raw` supply what csv and raw dumps don't carry.

    Raises:
        CommandError: If the file is missing, unreadable, of an unsupported type, or lacks a sample rate.
    """
    suffix = path.suffix.lower()
    if not path.is_file():
        raise CommandError(f"file not found: {path}")
    try:
        if suffix == ".wav":
            rate, data = wavfile.read(path)
            return Signal(to_float(data).reshape(len(data), -1), float(rate))
        if suffix == ".npz":
            with np.load(path) as z:
                samples = np.asarray(z["samples"], dtype=np.float64)
                return Signal(samples.reshape(len(samples), -1), float(z["fs"]))
        if suffix == ".csv":
            first = path.open(encoding="utf-8").readline()
            if first.startswith("# fs="):
                fs = float(first[5:])
            if fs is None:
                raise CommandError(f"{path.name}: sample rate unknown; add a '# fs=...' first line or pass --fs")
            return Signal(np.loadtxt(path, delimiter=",", comments="#", ndmin=2), fs)
        if suffix in RAW_SUFFIXES:
            if raw is None:
                raise CommandError(
                    f"{path.name} is a raw dump; convert it first with dsp.convert (--dtype --channels --fs)"
                )
            if fs is None:
                raise CommandError(f"{path.name}: raw dumps need --fs")
            data = np.fromfile(path, dtype=np.dtype(raw.dtype).newbyteorder("<" if raw.endian == "little" else ">"))
            if data.size % raw.channels:
                raise CommandError(f"{path.name}: {data.size} samples is not a multiple of {raw.channels} channels")
            data = data.reshape(raw.channels, -1).T if raw.layout == "planar" else data.reshape(-1, raw.channels)
            return Signal(to_float(data), fs)
    except (OSError, ValueError, KeyError) as e:
        raise CommandError(f"cannot read {path.name}: {e}") from None
    raise _unsupported(path)


def write_signal(
    path: Path, sig: Signal, *, dtype: str | None = None, layout: str = "interleaved", endian: str = "little"
) -> int:
    """Write a signal; returns the number of samples clipped by integer conversion."""
    suffix = path.suffix.lower()
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = 0
    try:
        if suffix == ".wav":
            if sig.fs != int(sig.fs):
                raise CommandError(f"WAV needs a whole-number sample rate, got {sig.fs}")
            data, clipped = from_float(sig.samples, dtype or "int16")
            if data.dtype not in (np.int16, np.int32, np.float32):
                raise CommandError("WAV output dtype must be int16, int32 or float32")
            wavfile.write(path, int(sig.fs), data)
        elif suffix == ".npz":
            np.savez(path, samples=sig.samples, fs=sig.fs)
        elif suffix == ".csv":
            np.savetxt(path, sig.samples, delimiter=",", fmt="%.9g", header=f"fs={sig.fs:g}", comments="# ")
        elif suffix in RAW_SUFFIXES:
            data, clipped = from_float(sig.samples, dtype or "int16")
            data = data.astype(data.dtype.newbyteorder("<" if endian == "little" else ">"))
            (data.T if layout == "planar" else data).copy().tofile(path)
        else:
            raise _unsupported(path)
    except (OSError, ValueError) as e:
        raise CommandError(f"cannot write {path.name}: {e}") from None
    return clipped
