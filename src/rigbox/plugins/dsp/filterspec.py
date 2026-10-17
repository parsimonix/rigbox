"""The filter manifest: what filter to design, validated by pydantic."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from scipy import signal as sps

from rigbox.core.registry import CommandError

if TYPE_CHECKING:
    from rigbox.core.context import Context

IIR_DESIGNS = {
    "butterworth": "butter",
    "chebyshev1": "cheby1",
    "chebyshev2": "cheby2",
    "elliptic": "ellip",
    "bessel": "bessel",
}


class FilterSpec(BaseModel):
    """A filter manifest: response type, design method, rates and edge frequencies."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    kind: Literal["iir", "fir"]
    response: Literal["lowpass", "highpass", "bandpass", "bandstop"]
    design: str | None = Field(
        None, description="iir: butterworth (default), chebyshev1, chebyshev2, elliptic, bessel; fir: window"
    )
    fs: float = Field(gt=0, description="Sample rate in Hz")
    cutoff: float | list[float] = Field(
        description="Edge frequency in Hz (two ascending values for band responses). Meaning depends on the design: "
        "-3 dB point for butterworth, passband edge for chebyshev1/elliptic, stopband edge for chebyshev2, "
        "approximate -3 dB for bessel, -6 dB point for fir"
    )
    order: int | None = Field(None, ge=1, description="iir only")
    taps: int | None = Field(None, ge=3, description="fir only; must be odd for highpass and bandstop")
    ripple_db: float | None = Field(None, gt=0, description="Passband ripple (chebyshev1, elliptic)")
    stopband_db: float | None = Field(None, gt=0, description="Stopband attenuation (chebyshev2, elliptic)")
    window: str = Field("hamming", description="fir window")
    numeric: Literal["float32", "q15", "q31"] = Field("float32", description="Number format the filter will run in")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        cutoffs, band = self.cutoffs, self.response in ("bandpass", "bandstop")
        if len(cutoffs) != (2 if band else 1):
            raise ValueError(f"{self.response} needs {2 if band else 1} cutoff value(s), got {len(cutoffs)}")
        if cutoffs != sorted(cutoffs) or not all(0 < c < self.fs / 2 for c in cutoffs):
            raise ValueError(f"cutoffs must be ascending and between 0 and fs/2 ({self.fs / 2:g} Hz)")
        if self.kind == "iir":
            self.design = self.design or "butterworth"
            if self.design not in IIR_DESIGNS:
                raise ValueError(f"iir design must be one of: {', '.join(IIR_DESIGNS)}")
            if self.order is None or self.taps is not None:
                raise ValueError("iir filters need 'order' (and no 'taps')")
            if self.design in ("chebyshev1", "elliptic") and self.ripple_db is None:
                raise ValueError(f"{self.design} needs 'ripple_db'")
            if self.design in ("chebyshev2", "elliptic") and self.stopband_db is None:
                raise ValueError(f"{self.design} needs 'stopband_db'")
        else:
            self.design = self.design or "window"
            if self.design != "window":
                raise ValueError("fir design must be 'window'")
            if self.taps is None or self.order is not None:
                raise ValueError("fir filters need 'taps' (and no 'order')")
            if self.response in ("highpass", "bandstop") and self.taps % 2 == 0:
                raise ValueError(f"{self.response} fir filters need an odd number of taps")
            try:
                sps.get_window(self.window, 8)
            except ValueError:
                raise ValueError(f"unknown window '{self.window}'") from None
        return self

    @property
    def cutoffs(self) -> list[float]:
        """The edge frequencies as a list, whether the manifest gave one value or two."""
        return [self.cutoff] if isinstance(self.cutoff, (int, float)) else list(self.cutoff)


def load_filter(path: Path) -> FilterSpec:
    """Load and validate the filter manifest at `path`.

    Raises:
        CommandError: If the file cannot be read or fails validation.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise CommandError(f"cannot read {path}: {e}") from None
    try:
        return FilterSpec.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'filter'}: {err['msg']}" for err in e.errors())
        raise CommandError(f"{path.name}: {problems}") from None


def find_filter(ctx: Context, ref: str) -> Path:
    """Return a filter manifest's path, given a file path or a name in the workspace manifests dir."""
    path = Path(ref)
    if not path.is_file() and ctx.workspace is not None:
        path = ctx.workspace.find_file(ctx.workspace.manifests_dir, ref) or path
    if not path.is_file():
        raise CommandError(f"filter manifest not found: {ref}")
    return path


def resolve_filter(ctx: Context, ref: str) -> FilterSpec:
    """Load the filter manifest named or pathed by `ref`."""
    return load_filter(find_filter(ctx, ref))
