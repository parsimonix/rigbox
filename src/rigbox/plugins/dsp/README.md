# DSP

Signals are files (`.wav`, `.csv`, `.npz`, or headerless `.raw`/`.bin` dumps), so every command works the same in the
REPL, one-shot runs and workflows. Samples are floats with full scale = 1.0; integer files map as value / 2^(bits-1),
so int16 is Q15 and int32 is Q31. Out-of-range samples are clipped and the clip count is reported.

Run these examples from the repository root:

```powershell
rigbox run dsp.gen sine tone.wav --freq 1000 --seconds 1 --amp 0.5    # sine square sawtooth triangle noise gaussian sweep impulse step dc
rigbox run dsp.inspect tone.wav                  # peak/rms/dBFS, clipping, fundamental, THD, THD+N
rigbox run dsp.spectrum tone.wav --peaks 3 --plot spectrum.png         # --out spectrum.csv for the whole curve
rigbox run dsp.resample tone.wav tone16k.wav 16000
rigbox run dsp.convert dump.raw dump.wav --fs 16000 --dtype int16 --channels 2 --layout planar
rigbox run dsp.convert tone.wav tone.raw --out-dtype int32 --endian big
```

Filters are YAML manifests (see [`examples/lowpass_1k.yaml`](../../../../examples/lowpass_1k.yaml)):
IIR (butterworth, chebyshev1/2, elliptic, bessel; stored as
second-order sections) or windowed FIR. `cutoff` means the -3 dB point for butterworth, the passband edge for
chebyshev1/elliptic, the stopband edge for chebyshev2, and the -6 dB point for FIR. `numeric` (`float32`, `q15`, `q31`) is
the format the filter will run in.

```powershell
rigbox run dsp.filter.design examples\lowpass_1k.yaml                  # coefficients
rigbox run dsp.filter.response examples\lowpass_1k.yaml --plot r.png   # gains, -3 dB corners, stability
rigbox run dsp.filter.apply examples\lowpass_1k.yaml in.wav out.wav    # filter a signal file
rigbox run dsp.filter.export examples\lowpass_1k.yaml --target cmsis   # lowpass_1k.h next to the manifest
rigbox run dsp.filter.export examples\lowpass_1k.yaml --format json --numeric float32
rigbox run dsp.filter.estimate examples\lowpass_1k.yaml --cycles-per-mac 1 --clock-mhz 120
```

Export writes C, JSON or CSV in a `generic` layout (conventional coefficient signs) or the `cmsis` layout for
`arm_biquad_cascade_df1_*` / `arm_fir_*` (negated feedback terms, q15 zero padding, shared post-shift, reversed FIR taps).
For q15/q31 it reports the worst-case magnitude-response error against the float design and whether the quantized poles
are still stable. Plots need the optional extra: `pip install "rigbox[plot]"`.

The estimator uses the cycles-per-MAC and clock you give it (there is no built-in table of cores). It assumes direct
form I biquads (5 MACs per stage) and a plain FIR tap loop.
