"""OI 4-quadrant sign-table conformance at the classifier level.

- dOI-rising quadrants (new longs / new shorts) VOTE for the matching trend state
- dOI-falling quadrants (short covering / long liquidation) are label-inert: same label as the
  no-fuel ablation, strictly lower confidence
- deadzone: fuel abstains, mild confidence trim
- OI present vs absent shifts scores/confidence in the direction the sign table demands
"""
import numpy as np

import features
import classifier
import scenarios
from conftest import FP_SMALL
from scenarios import WIN


def _run(df, **kw):
    feat = features.compute(df, FP_SMALL)
    return classifier.classify(feat, debug=True, **kw)


def test_rising_oi_supports_trend_scores():
    fused = _run(scenarios.trend_up("rising"))
    ablat = _run(scenarios.trend_up("rising"), use_fuel=False)
    assert (fused["score_TREND_UP"].iloc[WIN] > ablat["score_TREND_UP"].iloc[WIN]).all(), \
        "new-longs quadrant must raise the TREND_UP score vs no-fuel"

    fused_dn = _run(scenarios.trend_down("rising"))
    ablat_dn = _run(scenarios.trend_down("rising"), use_fuel=False)
    assert (fused_dn["score_TREND_DOWN"].iloc[WIN]
            > ablat_dn["score_TREND_DOWN"].iloc[WIN]).all()


def _scores(res):
    import classifier as cl
    return np.column_stack([res[f"score_{r}"].iloc[WIN].to_numpy(float) for r in cl.REGIMES])


def test_falling_oi_is_label_inert_confidence_trim():
    """Label-inertness is a property of the decision INPUTS: on dOI-falling bars the fused
    score vectors must equal the no-fuel ablation's exactly (fuel abstains from voting), and
    where the committed labels agree, confidence must be exactly x0.85."""
    for scn in [scenarios.trend_up("falling"), scenarios.trend_down("falling")]:
        fused = _run(scn)
        ablat = _run(scn, use_fuel=False)
        quad = features.compute(scn, FP_SMALL)["quadrant"].iloc[WIN]
        on = quad.isin([3, 4]).to_numpy()
        assert on.mean() > 0.9
        assert np.allclose(_scores(fused)[on], _scores(ablat)[on], atol=1e-12), \
            "dOI-falling quadrants must not move any state score"
        same = (fused["regime"].iloc[WIN].to_numpy(object)
                == ablat["regime"].iloc[WIN].to_numpy(object)) & on
        assert same.any()
        cf = fused["confidence"].iloc[WIN].to_numpy(float)[same]
        ca = ablat["confidence"].iloc[WIN].to_numpy(float)[same]
        assert np.allclose(cf, ca * 0.85, atol=1e-9), \
            "dOI-falling quadrants must trim confidence by exactly x0.85"


def test_deadzone_mild_trim():
    df = scenarios.trend_up("flat")
    fused = _run(df)
    ablat = _run(df, use_fuel=False)
    quad = features.compute(df, FP_SMALL)["quadrant"].iloc[WIN]
    on = (quad == 0).to_numpy()
    assert on.mean() > 0.9
    assert np.allclose(_scores(fused)[on], _scores(ablat)[on], atol=1e-12)
    same = (fused["regime"].iloc[WIN].to_numpy(object)
            == ablat["regime"].iloc[WIN].to_numpy(object)) & on
    assert same.any()
    f = fused["confidence"].iloc[WIN].to_numpy(float)[same]
    a = ablat["confidence"].iloc[WIN].to_numpy(float)[same]
    assert np.allclose(f, a * 0.95, atol=1e-9), "deadzone = x0.95 confidence, nothing else"


def test_oi_presence_shifts_output():
    """Same prices, with vs without OI: label and/or confidence must differ somewhere."""
    df = scenarios.trend_up("rising")
    fused = _run(df)
    no_oi = df.drop(columns=["oi", "oi_lag1"]).assign(oi_fresh=False)
    degraded = _run(no_oi)
    diff = (fused["regime"].iloc[WIN].to_numpy(object)
            != degraded["regime"].iloc[WIN].to_numpy(object)).any() or \
        not np.allclose(fused["confidence"].iloc[WIN], degraded["confidence"].iloc[WIN])
    assert diff
