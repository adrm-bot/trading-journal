"""THE look-ahead test. Any future reference anywhere in the pipeline must fail it.

Operates on the RAW three tables (klines / metrics / funding) from the real archive, sliced to
2023-07 .. 2024-04 (contains month-file boundaries, funding times, label transitions, and the
2024-02-16 OI outage):

- truncate mode: cut ALL three raw tables at wall-clock t*, rerun the full pipeline
  (assemble -> features -> classify F + B2 + B1); every output at ts <= t* must EXACTLY match
  the full run (labels/events: no tolerance; floats: atol 1e-12)
- poison mode: keep full length but replace every row after t* in all three tables with
  garbage; same assertion
- canary: a deliberately leaky pipeline (oi shifted -1 candle after the join) MUST be caught
  by the same machinery — proves the test can fail

Cut points are adversarial, not uniform-random: month zip boundaries, a funding boundary,
inside the OI gap, and immediately after real label transitions.
"""
import numpy as np
import pandas as pd
import pytest

import build_dataset as bd
import baseline
import classifier
import features

SYMBOL = "BTCUSDT"
SLICE = ("2023-07-01", "2024-04-30")
BAR = pd.Timedelta(minutes=15)


@pytest.fixture(scope="module")
def raw():
    if not (bd.RAW / SYMBOL / "metrics").exists():
        pytest.skip("raw archive not downloaded")
    kraw = bd.read_klines_raw(SYMBOL, *SLICE)
    m, _, _ = bd.read_metrics(SYMBOL, *SLICE)
    fu = bd.read_funding(SYMBOL, *SLICE)
    return kraw, m, fu


def pipeline(kraw, m, fu, leak=False):
    df = bd.assemble(kraw, m, fu)
    if leak:
        df["oi"] = df["oi"].shift(-1)  # deliberate future reference (canary)
    feat = features.compute(df)
    f = classifier.classify(feat)
    b2 = classifier.classify(feat, use_fuel=False)
    b1 = baseline.classify_b1(feat)
    return f, b2, b1


def cut(kraw, m, fu, tstar):
    return (kraw[kraw["open_ts"] + BAR <= tstar],
            m[m["snap_ts"] <= tstar],
            fu[fu["event_ts"] <= tstar])


def poison(kraw, m, fu, tstar):
    k2, m2, f2 = kraw.copy(), m.copy(), fu.copy()
    kf = k2["open_ts"] + BAR > tstar
    for col, fac in [("open", 7.0), ("high", 9.0), ("low", 0.5), ("close", 7.0), ("volume", 100.0)]:
        k2.loc[kf, col] *= fac
    m2.loc[m2["snap_ts"] > tstar, "oi"] *= 13.0
    f2.loc[f2["event_ts"] > tstar, "funding"] = 0.05
    return k2, m2, f2


def _labels(arr) -> np.ndarray:
    a = arr.to_numpy(object)
    return np.array(["__NA__" if pd.isna(x) else str(x) for x in a], dtype=object)


def _mismatches(full, sub, upto):
    """Compare (f, b2, b1) tuples on rows <= upto; return list of mismatch descriptions."""
    out = []
    for name, a, b in [("F", full[0], sub[0]), ("B2", full[1], sub[1])]:
        ai, bi = a.loc[a.index <= upto], b.loc[b.index <= upto]
        if not ai.index.equals(bi.index):
            out.append(f"{name}: index mismatch")
            continue
        neq = _labels(ai["regime"]) != _labels(bi["regime"])
        if neq.any():
            out.append(f"{name}: {neq.sum()} label mismatches, first at {ai.index[neq][0]}")
        ca = ai["confidence"].to_numpy(float)
        cb = bi["confidence"].to_numpy(float)
        cneq = ~(np.isclose(ca, cb, atol=1e-12) | (np.isnan(ca) & np.isnan(cb)))
        if cneq.any():
            out.append(f"{name}: {cneq.sum()} confidence mismatches, first at {ai.index[cneq][0]}")
    neq = _labels(full[2].loc[full[2].index <= upto]) != _labels(sub[2].loc[sub[2].index <= upto])
    if neq.any():
        out.append(f"B1: {neq.sum()} label mismatches")
    return out


def cut_points(full_f):
    pts = [pd.Timestamp("2023-08-01 00:00", tz="UTC"),   # month zip boundary
           pd.Timestamp("2024-01-01 00:00", tz="UTC"),   # year/month boundary
           pd.Timestamp("2023-09-10 16:00", tz="UTC"),   # funding boundary
           pd.Timestamp("2024-02-16 18:00", tz="UTC")]   # inside the OI outage
    lab = full_f["regime"]
    trans = lab.notna() & lab.shift().notna() & (lab != lab.shift())
    t_idx = lab.index[trans]
    t_idx = t_idx[t_idx > pd.Timestamp("2023-09-01", tz="UTC")]
    pts += [t_idx[i] + BAR for i in (0, len(t_idx) // 2, len(t_idx) - 1)]
    return pts


@pytest.fixture(scope="module")
def full_run(raw):
    return pipeline(*raw)


def test_truncate_invariance(raw, full_run):
    for tstar in cut_points(full_run[0]):
        sub = pipeline(*cut(*raw, tstar))
        mm = _mismatches(full_run, sub, tstar)
        assert not mm, f"future data reached outputs at cut {tstar}: {mm}"


def test_poison_invariance(raw, full_run):
    for tstar in cut_points(full_run[0]):
        sub = pipeline(*poison(*raw, tstar))
        mm = _mismatches(full_run, sub, tstar)
        assert not mm, f"poisoned future data reached outputs at cut {tstar}: {mm}"


def test_canary_planted_leak_is_caught(raw):
    """The machinery itself must be able to fail: a -1 candle OI shift is a real leak and at
    least one cut point must expose it. If this passes trivially the suite is worthless."""
    full_leaky = pipeline(*raw, leak=True)
    caught = 0
    for tstar in cut_points(full_leaky[0]):
        sub_leaky = pipeline(*cut(*raw, tstar), leak=True)
        if _mismatches(full_leaky, sub_leaky, tstar):
            caught += 1
    assert caught > 0, "planted future-reference was NOT caught — look-ahead test is blind"
