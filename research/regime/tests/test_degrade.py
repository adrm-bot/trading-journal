"""Degrade path: missing oi/funding columns -> fuel skipped + penalty + flag, no crash."""
import numpy as np

import features
import classifier
import scenarios
from conftest import FP_SMALL


def test_missing_oi_column_no_crash():
    df = scenarios.trend_up("rising").drop(columns=["oi", "oi_lag1", "oi_fresh"])
    feat = features.compute(df, FP_SMALL)  # oi columns absent entirely
    res = classifier.classify(feat)
    ok = res["regime"].notna()
    assert ok.any()
    assert (~res["fuel_available"]).all()
    assert ((res.loc[ok, "confidence"] >= 0) & (res.loc[ok, "confidence"] <= 1)).all()


def test_nan_oi_column_degrades_with_penalty():
    df = scenarios.trend_up("rising")
    df["oi"] = np.nan
    df["oi_fresh"] = False
    feat = features.compute(df, FP_SMALL)
    degraded = classifier.classify(feat)
    ablation = classifier.classify(feat, use_fuel=False)  # B2: same axes, NO penalty
    ok = degraded["regime"].notna()
    # same labels (identical information), penalized confidence
    assert (degraded.loc[ok, "regime"] == ablation.loc[ok, "regime"]).all()
    ratio = (degraded.loc[ok, "confidence"] / ablation.loc[ok, "confidence"]).dropna()
    assert np.allclose(ratio, classifier.DEFAULT_CP.degrade_penalty, atol=1e-9)


def test_missing_funding_no_crash():
    df = scenarios.trend_up("rising").drop(columns=["funding"])
    feat = features.compute(df, FP_SMALL)
    res = classifier.classify(feat)
    assert res["regime"].notna().any()
