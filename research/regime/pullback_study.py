#!/usr/bin/env python3
"""PRE-REGISTERED validation of the user's pullback insight (2026-07-05, before results):

  "HTF(1H) TREND_UP + LTF(15m) in SQUEEZE/RANGE (esp. after a down-leg) is a good
   pullback-long habitat."

Conditions (fixed before looking at any output):
  H1 track   = full classifier on 15m data resampled to 1H (wall-clock scaled windows:
               q_window=720, min_fuel=480, k=2, burn=840), aligned to 15m bars via
               merge_asof backward on the last COMPLETED 1H candle (causal).
  L_A  = H1 TREND_UP  and 15m in {SQUEEZE, RANGE}
  L_B  = L_A and 15m regime included TREND_DOWN within the prior 32 bars ("after a dip")
  S_A / S_B symmetric with TREND_DOWN / prior TREND_UP.
  Baselines: unconditional, and H1-trend alone (is the LTF pullback filter adding anything?)
  Metrics: forward 16-bar (4h) and 96-bar (24h) median return %, share_up, n.
  Exclusions: burn-in, data-gap mask, last h bars. Overlapping windows -> descriptive stats
  only (no clustered inference) — same honesty rule as the posthoc stage.

Usage: python pullback_study.py BTCUSDT ETHUSDT
"""
import json
import sys

import numpy as np
import pandas as pd

import classifier
import features
import run_all
from features import FeatureParams

H1_FP = FeatureParams(q_window=720, q_min_periods_fuel=480, oi_lookback=2, burn_in=840)
HORIZONS = (16, 96)
LOOKBACK_DIP = 32


def h1_track(df15: pd.DataFrame) -> pd.Series:
    o = df15["open"].resample("1h").first()
    h = df15["high"].resample("1h").max()
    l = df15["low"].resample("1h").min()
    c = df15["close"].resample("1h").last()
    v = df15["volume"].resample("1h").sum()
    oi = df15["oi"].resample("1h").last()
    fresh = df15["oi_fresh"].resample("1h").last()
    miss = df15["bar_missing"].resample("1h").max()
    fund = df15["funding"].resample("1h").last() if "funding" in df15.columns else None
    h1 = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v,
                       "oi": oi, "oi_fresh": fresh.astype(bool), "bar_missing": miss.astype(bool)})
    if fund is not None:
        h1["funding"] = fund
    # resample stamps at period START; our convention is CLOSE time
    h1.index = h1.index + pd.Timedelta(hours=1)
    feat = features.compute(h1, H1_FP)
    return classifier.classify(feat)["regime"]


def stats(mask: np.ndarray, c: np.ndarray, h: int) -> dict:
    m = mask.copy()
    m[-h:] = False
    r = (np.roll(c, -h) / c - 1.0)[m]
    if len(r) == 0:
        return {"n": 0}
    return {"n": int(m.sum()), "med_pct": round(float(np.median(r)) * 100, 3),
            "share_up": round(float((r > 0).mean()), 3)}


def run(symbol: str) -> dict:
    df, excluded, _ = run_all.load(symbol)
    feat = features.compute(df)
    f = classifier.classify(feat)
    lab15 = run_all.trim(f)["regime"]
    idx = lab15.index
    c = df["close"].iloc[run_all.BURN:].to_numpy(float)
    exc = run_all.full_excluded(excluded, run_all.trim(f))

    h1 = h1_track(df)
    h1_on_15 = pd.merge_asof(
        pd.DataFrame(index=idx).reset_index(),
        h1.rename("h1").reset_index().rename(columns={"index": "ts"}),
        on="ts", direction="backward").set_index("ts")["h1"]

    l15 = lab15.to_numpy(object)
    lh1 = h1_on_15.to_numpy(object)
    ok = (~pd.isna(l15)) & (~pd.isna(lh1)) & ~exc
    in_sqrg = ok & ((l15 == "SQUEEZE") | (l15 == "RANGE"))
    h1_up = ok & (lh1 == "TREND_UP")
    h1_dn = ok & (lh1 == "TREND_DOWN")

    dip_dn = pd.Series(l15 == "TREND_DOWN").rolling(LOOKBACK_DIP, min_periods=1).max() \
        .shift(1).fillna(0).astype(bool).to_numpy()
    dip_up = pd.Series(l15 == "TREND_UP").rolling(LOOKBACK_DIP, min_periods=1).max() \
        .shift(1).fillna(0).astype(bool).to_numpy()

    conds = {
        "uncond": ok,
        "H1_up_only": h1_up,
        "L_A (H1up+15m sq/rg)": h1_up & in_sqrg,
        "L_B (A + dip<=32bars)": h1_up & in_sqrg & dip_dn,
        "H1_dn_only": h1_dn,
        "S_A (H1dn+15m sq/rg)": h1_dn & in_sqrg,
        "S_B (A + pop<=32bars)": h1_dn & in_sqrg & dip_up,
    }
    out = {}
    for name, m in conds.items():
        out[name] = {str(h): stats(m, c, h) for h in HORIZONS}
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    res = {}
    for sym in sys.argv[1:] or ["BTCUSDT"]:
        res[sym] = run(sym)
        print(f"\n[{sym}] 눌림목 인사이트 검증 (fwd 4h / 24h)")
        for name, d in res[sym].items():
            s16, s96 = d["16"], d["96"]
            if s16.get("n", 0) == 0:
                print(f"  {name:24s} n=0")
                continue
            print(f"  {name:24s} n={s16['n']:6d}  4h: {s16['med_pct']:+.3f}% ↑{s16['share_up']:.0%}"
                  f"   24h: {s96['med_pct']:+.3f}% ↑{s96['share_up']:.0%}")
    run_all.RESULTS.mkdir(exist_ok=True)
    (run_all.RESULTS / "pullback_study.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
