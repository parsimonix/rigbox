"""Quantize a filter's coefficients and write them as C, JSON or CSV, in a generic or CMSIS-DSP layout.

CMSIS-DSP conventions (checked against its documentation): biquad feedback terms are *added*, so a1/a2 are negated
relative to the usual design-tool sign; q15 biquads carry a zero after b0 in each 6-value stage; fixed-point
coefficients must lie in [-1, 1) with a shared `postShift` (output is shifted left by it); FIR coefficients are stored
time-reversed, and arm_fir_q15 needs an even tap count of at least 4.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy import signal as sps

from rigbox.core.registry import CommandError
from rigbox.plugins.dsp.design import Design, pole_radii, response

WORD_FRAC = {"q15": 15, "q31": 31}  # fractional bits; the word is frac + 1 bits wide
C_TYPES = {
    "generic": {"float32": "float", "q15": "int16_t", "q31": "int32_t"},
    "cmsis": {"float32": "float32_t", "q15": "q15_t", "q31": "q31_t"},
}
ERROR_FLOOR = 1e-4  # compare responses only where the filter passes more than -80 dB


@dataclass
class Coefficients:
    """Quantized coefficients plus how they were laid out and how far the response moved."""

    values: list[int] | list[float]  # flat, in export order (floats for float32, ints for q15/q31)
    numeric: str
    target: str
    per_stage: int | None  # values per biquad stage; None for FIR
    count: int  # stages (IIR) or taps (FIR) in the design itself
    post_shift: int
    max_response_error_db: float
    stable: bool


def _post_shift(max_abs: float) -> int:
    return 0 if max_abs < 1 else int(np.floor(np.log2(max_abs))) + 1


def _quantize(values: NDArray[np.float64], numeric: str) -> NDArray[np.int64]:
    frac = WORD_FRAC[numeric]
    return np.clip(np.rint(values * 2**frac), -(2**frac), 2**frac - 1).astype(np.int64)


def _balance_gains(sos: NDArray[np.float64]) -> NDArray[np.float64]:
    """Rescale each stage's numerator so all stages have the same peak gain, leaving the response unchanged.

    scipy puts nearly all the gain in the first stage, which leaves other numerators tiny (or huge) and loses
    precision in Q15/Q31. Headroom against overflow is still the implementer's job.
    """
    peaks = np.array([np.abs(sps.sosfreqz(s[None, :], worN=2048)[1]).max() for s in sos])
    scale = np.prod(peaks) ** (1 / len(sos)) / peaks
    balanced = sos.copy()
    balanced[:, :3] *= scale[:, None]
    return balanced


def _response_error(original: Design, quantized: Design) -> tuple[float, bool]:
    freqs = np.linspace(0, original.spec.fs / 2, 1024)
    h, hq = response(original, freqs), response(quantized, freqs)
    keep = np.abs(h) > ERROR_FLOOR
    error = np.max(np.abs(20 * np.log10((np.abs(hq[keep]) + 1e-12) / np.abs(h[keep])))) if keep.any() else 0.0
    stable = bool(pole_radii(quantized.sos).max() < 1.0) if quantized.sos is not None else True
    return round(float(error), 4), stable


def export(d: Design, target: str) -> Coefficients:
    """Quantize the design to its manifest's number format and arrange it for `target` (generic or cmsis).

    Raises:
        CommandError: If FIR taps are too large for the Q format.
    """
    numeric, cmsis = d.spec.numeric, target == "cmsis"
    if d.sos is not None:
        sos = d.sos if numeric == "float32" else _balance_gains(d.sos)
        rows = np.hstack([sos[:, :3], -sos[:, 4:6] if cmsis else sos[:, 4:6]])  # b0 b1 b2 a1 a2
        stored: NDArray[np.float32] | NDArray[np.int64]
        if numeric == "float32":
            shift, stored = 0, rows.astype(np.float32)
            restored = stored.astype(np.float64)
        else:
            shift = _post_shift(float(np.abs(rows).max()))
            stored = _quantize(rows / 2**shift, numeric)
            restored = stored / 2 ** WORD_FRAC[numeric] * 2**shift
        sos_hat = np.hstack([restored[:, :3], np.ones((len(rows), 1)), -restored[:, 3:] if cmsis else restored[:, 3:]])
        error, stable = _response_error(d, Design(d.spec, sos=sos_hat))
        if cmsis and numeric == "q15":
            stored = np.insert(stored, 1, 0, axis=1)  # CMSIS q15 pads each stage with a zero after b0
        return Coefficients(stored.ravel().tolist(), numeric, target, stored.shape[1], len(rows), shift, error, stable)

    taps = d.taps
    assert taps is not None  # a Design has either sos or taps
    if numeric == "float32":
        stored = taps.astype(np.float32)
        restored = stored.astype(np.float64)
    else:
        if np.abs(taps).max() >= 1:
            raise CommandError(
                "filter taps reach +-1 or more, which the Q format cannot hold; scale the design or use float32"
            )
        stored = _quantize(taps, numeric)
        restored = stored / 2 ** WORD_FRAC[numeric]
    error, stable = _response_error(d, Design(d.spec, taps=restored))
    values = stored.tolist()
    if cmsis:
        if numeric == "q15" and (len(values) % 2 or len(values) < 4):
            values += [0] * (max(4, len(values) + len(values) % 2) - len(values))
        values.reverse()  # CMSIS FIR stores b[numTaps-1] ... b[0]
    return Coefficients(values, numeric, target, None, len(taps), 0, error, stable)


def _macro_name(name: str) -> str:
    cleaned = re.sub(r"\W", "_", name).upper()
    return f"_{cleaned}" if cleaned[0].isdigit() else cleaned


def _literal(value: float, numeric: str) -> str:
    if numeric == "float32":
        text = f"{value:.9g}"
        return text + (".0" if re.fullmatch(r"-?\d+", text) else "") + "f"
    return "(-2147483647 - 1)" if value == -(2**31) else str(value)


def to_c(c: Coefficients, d: Design) -> str:
    """Render the coefficients as a C header."""
    spec, name = d.spec, _macro_name(d.spec.name)
    size = f"order {spec.order}" if spec.kind == "iir" else f"{spec.taps} taps"
    design = f"{spec.design} {spec.response} {spec.kind}, {size}"
    lines = [f"/* Generated by rigbox from filter '{spec.name}': {design}, fs {spec.fs:g} Hz, {spec.numeric}. */"]
    if spec.kind == "iir":
        if c.target == "cmsis":
            zero = "0, " if c.per_stage == 6 else ""
            lines.append(
                f"/* Per stage: b0, {zero}b1, b2, a1, a2. a1/a2 are negated for CMSIS-DSP (feedback is added). */"
            )
        else:
            lines.append(
                "/* Per stage: b0, b1, b2, a1, a2 with "
                "y[n] = b0 x[n] + b1 x[n-1] + b2 x[n-2] - a1 y[n-1] - a2 y[n-2]. */"
            )
    elif c.target == "cmsis":
        lines.append("/* Coefficients are in time-reversed order, as CMSIS-DSP expects. */")
    lines += [f"#ifndef {name}_H", f"#define {name}_H", ""]
    lines.append('#include "arm_math.h"' if c.target == "cmsis" else "#include <stdint.h>")
    lines.append("")
    if spec.kind == "iir":
        lines.append(f"#define {name}_NUM_STAGES {c.count}")
        if c.numeric != "float32":
            lines.append(f"#define {name}_POST_SHIFT {c.post_shift}  /* shift the accumulator left by this */")
        if c.target == "cmsis":
            lines.append(f"#define {name}_STATE_LEN {4 * c.count}")
    else:
        taps = len(c.values)
        lines.append(f"#define {name}_NUM_TAPS {taps}")
        if c.target == "cmsis":
            lines.append(f"#define {name}_STATE_LEN(block_size) ({taps} + (block_size) - 1)")
    lines += ["", f"static const {C_TYPES[c.target][c.numeric]} {name}_COEFFS[{len(c.values)}] = {{"]
    width = c.per_stage or 8
    for i in range(0, len(c.values), width):
        chunk = ", ".join(_literal(v, c.numeric) for v in c.values[i : i + width])
        lines.append(f"    {chunk}{',' if i + width < len(c.values) else ''}")
    lines += ["};", "", f"#endif /* {name}_H */", ""]
    return "\n".join(lines)


def to_json(c: Coefficients, d: Design) -> str:
    """Render the coefficients and their metadata as JSON."""
    spec = d.spec
    return json.dumps(
        {
            "name": spec.name, "kind": spec.kind, "numeric": c.numeric, "target": c.target, "fs": spec.fs,
            "stages" if spec.kind == "iir" else "taps": c.count,
            "values_per_stage": c.per_stage, "post_shift": c.post_shift,
            "max_response_error_db": c.max_response_error_db, "stable": c.stable,
            "coefficients": c.values,
        },
        indent=2,
    ) + "\n"


def to_csv(c: Coefficients) -> str:
    """Render the coefficients as CSV, one row per biquad stage or FIR tap."""
    if c.per_stage is None:
        return "index,value\n" + "".join(f"{i},{v}\n" for i, v in enumerate(c.values))
    rows = [c.values[i : i + c.per_stage] for i in range(0, len(c.values), c.per_stage)]
    return "stage,coefficients\n" + "".join(f"{i},{' '.join(map(str, r))}\n" for i, r in enumerate(rows))
