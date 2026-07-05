"""5-state reachability on synthetics + occupancy floor on real data + confidence contract."""
import features
import classifier
import scenarios
from conftest import FP_SMALL

def _label_win(df):
    feat = features.compute(df, FP_SMALL)
    res = classifier.classify(feat)
    return res["regime"].iloc[scenarios.WIN], res


def test_each_state_reachable():
    for scn, want in [
        (scenarios.trend_up(), "TREND_UP"),
        (scenarios.trend_down(), "TREND_DOWN"),
        (scenarios.squeeze(), "SQUEEZE"),
        (scenarios.chop(), "CHOP"),
        (scenarios.range_mid(), "RANGE"),
    ]:
        lab, _ = _label_win(scn)
        share = (lab == want).mean()
        # RANGE: a pure random walk legitimately grazes the SQUEEZE/CHOP ramps now and then
        floor = 0.7 if want == "RANGE" else 0.8
        assert share > floor, f"expected {want} in onset window, got {lab.value_counts().to_dict()}"


def test_confidence_contract():
    for scn in [scenarios.trend_up(), scenarios.squeeze(), scenarios.chop()]:
        feat = features.compute(scn, FP_SMALL)
        res = classifier.classify(feat)
        ok = res["regime"].notna()
        c = res.loc[ok, "confidence"]
        assert ((c >= 0) & (c <= 1)).all()
        assert c.notna().all()
        # emits exactly one of the 5 states for every classified candle
        assert set(res.loc[ok, "regime"].unique()) <= set(classifier.REGIMES)


def test_confidence_is_continuous():
    feat = features.compute(scenarios.range_mid(), FP_SMALL)
    res = classifier.classify(feat)
    ok = res["confidence"].notna()
    n = ok.sum()
    distinct = res.loc[ok, "confidence"].round(9).nunique()
    assert distinct / n * 1000 > 50, "confidence must not collapse onto a few mass points"


def test_output_contract_records():
    feat = features.compute(scenarios.trend_up(), FP_SMALL)
    res = classifier.classify(feat)
    recs = classifier.to_records(res)
    assert recs, "no classified candles"
    assert set(recs[0].keys()) == {"regime", "confidence", "timestamp"}


def test_real_data_occupancy_floor(btc_df):
    feat = features.compute(btc_df)
    res = classifier.classify(feat)
    scored = res.iloc[features.DEFAULT_FP.burn_in:]
    occ = scored["regime"].value_counts(normalize=True)
    for state in classifier.REGIMES:
        assert occ.get(state, 0.0) > 0.02, f"{state} occupancy {occ.get(state, 0):.3%} < 2% floor"
    assert scored["regime"].isna().sum() == 0, "every candle after burn-in must be classified"
