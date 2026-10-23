"""Minimal stand-in for `resampy` (which needs numba, ~100 MB) used only by
Matchering's checker when a file is not 44.1 kHz. MixChecker always converts
to 44.1 kHz before calling Matchering, so this is a safety net: polyphase
resampling from scipy."""
from fractions import Fraction

from scipy.signal import resample_poly

__version__ = "0.4.2-scipy-shim"


def resample(x, sr_orig, sr_new, axis=-1, **kwargs):
    f = Fraction(int(sr_new), int(sr_orig)).limit_denominator(1000)
    return resample_poly(x, f.numerator, f.denominator, axis=axis)
