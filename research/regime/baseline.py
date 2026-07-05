#!/usr/bin/env python3
"""B1 baseline: the classic ADX regime detector, nothing else.

TREND_UP   if ADX >= 25 and DI+ > DI-
TREND_DOWN if ADX >= 25 and DI+ < DI-
SIDEWAYS   otherwise

Deliberately dumb and slow-to-confirm: this is the reference clock the fused classifier is
measured against. Computed from the SAME feature frame and stamped at the same candle-close
timestamps as the fused track (a per-track stamping difference would bias every lead by a
constant).
"""
import numpy as np
import pandas as pd

ADX_CUT = 25.0


def classify_b1(feat: pd.DataFrame) -> pd.Series:
    adx = feat["adx"].to_numpy(float)
    di = feat["di_diff"].to_numpy(float)
    valid = ~(np.isnan(adx) | np.isnan(di))
    lab = np.where(~valid, None,
                   np.where((adx >= ADX_CUT) & (di > 0), "TREND_UP",
                            np.where((adx >= ADX_CUT) & (di < 0), "TREND_DOWN", "SIDEWAYS")))
    return pd.Series(lab, index=feat.index, name="regime")
