"""Each axis must return the expected sign/reading on obvious synthetic segments.

Assertions read scenarios.WIN (40..120 bars after onset): indicators have re-converged there
but the past-only percentile window is not yet saturated by the scenario itself.
"""
import numpy as np

import features
import scenarios
from conftest import FP_SMALL
from scenarios import WIN


def win(feat, col):
    return feat[col].iloc[WIN]


def test_direction_axis_signs():
    up = features.compute(scenarios.trend_up(), FP_SMALL)
    dn = features.compute(scenarios.trend_down(), FP_SMALL)
    assert (win(up, "m") > 0).all() and (win(up, "adx") > 25).all()
    assert (win(up, "di_diff") > 0).all()
    assert (win(dn, "m") < 0).all() and (win(dn, "adx") > 25).all()
    assert (win(dn, "di_diff") < 0).all()


def test_volatility_axis_percentiles():
    sq = features.compute(scenarios.squeeze(), FP_SMALL)
    ch = features.compute(scenarios.chop(), FP_SMALL)
    rg = features.compute(scenarios.range_mid(), FP_SMALL)
    assert (win(sq, "vol_pct") < 0.15).all(), "squeeze onset must rank in the low-vol quantile"
    assert (win(ch, "vol_pct") > 0.85).all(), "chop onset must rank in the high-vol quantile"
    # a stationary walk's past-only percentile is ~uniform in the LONG run (short windows can
    # legitimately sit anywhere) — assert distributional properties over the whole series
    u = rg["vol_pct"].dropna()
    assert 0.35 < u.mean() < 0.65, "steady walk vol percentile must center near 0.5"
    assert u.between(0.15, 0.85).mean() > 0.55


def test_fuel_axis_quadrants():
    q1 = features.compute(scenarios.trend_up("rising"), FP_SMALL)
    q2 = features.compute(scenarios.trend_down("rising"), FP_SMALL)
    q3 = features.compute(scenarios.trend_up("falling"), FP_SMALL)
    q4 = features.compute(scenarios.trend_down("falling"), FP_SMALL)
    dz = features.compute(scenarios.trend_up("flat"), FP_SMALL)
    assert (win(q1, "quadrant") == 1).mean() > 0.9  # dOI up, price up: new longs
    assert (win(q2, "quadrant") == 2).mean() > 0.9  # dOI up, price down: new shorts
    assert (win(q3, "quadrant") == 3).mean() > 0.9  # dOI down, price up: short covering
    assert (win(q4, "quadrant") == 4).mean() > 0.9  # dOI down, price down: long liquidation
    assert (win(dz, "quadrant") == 0).mean() > 0.9  # ~0 dOI: deadzone / no fuel
    assert win(dz, "fuel_defined").all()


def test_causality_smoke_no_centered_windows():
    """features at t must not move when only future closes change (spot check)."""
    df = scenarios.range_mid()
    feat_full = features.compute(df, FP_SMALL)
    cut = len(df) - 50
    df2 = df.copy()
    df2.iloc[cut:, df2.columns.get_loc("close")] *= 3.0
    feat_cut = features.compute(df2, FP_SMALL)
    a = feat_full.iloc[:cut][["vol_pct", "adx", "m", "doi"]]
    b = feat_cut.iloc[:cut][["vol_pct", "adx", "m", "doi"]]
    assert np.allclose(a.fillna(-9), b.fillna(-9), atol=1e-12)
