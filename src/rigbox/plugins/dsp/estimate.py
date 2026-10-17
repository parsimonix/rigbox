"""Resource estimate for running a filter. Target numbers (cycles per MAC, clock) come from the user, not a table."""

from __future__ import annotations

from rigbox.plugins.dsp.design import Design

WORD_BYTES = {"float32": 4, "q15": 2, "q31": 4}


def estimate(
    d: Design, *, cycles_per_mac: float, overhead_cycles: float, clock_mhz: float | None
) -> dict[str, float | None]:
    """Estimate per-sample cycles, coefficient/state memory and (given a clock) CPU load.

    Assumes biquads run as direct form I (5 multiply-accumulates and 4 state words per stage) and FIR filters
    as a plain tap loop (no symmetry tricks). Accumulator width is not counted in memory.
    """
    if d.sos is not None:
        stages = len(d.sos)
        macs, coeff_words, state_words = 5 * stages, 5 * stages, 4 * stages
    else:
        assert d.taps is not None  # a Design has either sos or taps
        taps = len(d.taps)
        macs, coeff_words, state_words = taps, taps, taps - 1
    word = WORD_BYTES[d.spec.numeric]
    cycles = macs * cycles_per_mac + overhead_cycles
    row: dict[str, float | None] = {
        "macs_per_sample": macs,
        "cycles_per_sample": round(cycles, 2),
        "coeff_bytes": coeff_words * word,
        "state_bytes": state_words * word,
        "load_percent": None,
        "max_sample_rate_hz": None,
    }
    if clock_mhz is not None:
        row["load_percent"] = round(cycles * d.spec.fs / (clock_mhz * 1e6) * 100, 2)
        row["max_sample_rate_hz"] = round(clock_mhz * 1e6 / cycles, 1)
    return row
