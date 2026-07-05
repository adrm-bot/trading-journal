#!/usr/bin/env python3
"""PRE-REGISTERED (2026-07-05, before results): user's eyeball hypothesis from chart —

  "During consolidation (SQUEEZE/RANGE) near its tail, a CONFIDENCE COLLAPSE while the
   higher TF is trending = trend re-ignition trigger."

Conditions (fixed before output):
  CT_L = H1 TREND_UP  and 15m in {SQUEEZE, RANGE} and collapse event fires at t
         (collapse = run_all.collapse_events: conf < own rolling q20, 2-bar debounce,
          gap/availability masked — the exact production definition)
  CT_S = symmetric with H1 TREND_DOWN.
  Reference rows: L_A / S_A (same habitat WITHOUT collapse — from pullback_study, recomputed
  here for the identical sample), unconditional.
  Metrics: forward 16-bar (4h) and 96-bar (24h) median return %, share_up, n.
  Honesty: collapse events are sparse -> n will be small; overlapping windows -> descriptive.

Usage: python continuation_study.py BTCUSDT ETHUSDT
"""
import json
import sys

import numpy as np
import pandas as pd

import classifier
import features
import run_all
from pullback_study import h1_track, stats, HORIZONS


def run(symbol: str) -> dict:
    df, excluded, _ = run_all.load(symbol)
    feat = features.compute(df)
    f = classifier.classify(feat)
    ft = run_all.trim(f)
    lab15 = ft["regime"]
    idx = lab15.index
    c = df["close"].iloc[run_all.BURN:].to_numpy(float)
    exc = run_all.full_excluded(excluded, ft)

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

    ev = run_all.collapse_events(ft["confidence"], ft["fuel_available"], exc)
    col = np.zeros(len(idx), dtype=bool)
    col[ev] = True

    conds = {
        "uncond": ok,
        "L_A (H1up+sq/rg, no-collapse ref)": h1_up & in_sqrg & ~col,
        "CT_L (H1up+sq/rg+COLLAPSE)": h1_up & in_sqrg & col,
        "S_A (H1dn+sq/rg, no-collapse ref)": h1_dn & in_sqrg & ~col,
        "CT_S (H1dn+sq/rg+COLLAPSE)": h1_dn & in_sqrg & col,
    }
    return {name: {str(h): stats(m, c, h) for h in HORIZONS} for name, m in conds.items()}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    res = {}
    for sym in sys.argv[1:] or ["BTCUSDT"]:
        res[sym] = run(sym)
        print(f"\n[{sym}] 횡보 끝자락 확신급락 = 재점화 트리거? (fwd 4h / 24h)")
        for name, d in res[sym].items():
            s16, s96 = d["16"], d["96"]
            if s16.get("n", 0) == 0:
                print(f"  {name:36s} n=0")
                continue
            print(f"  {name:36s} n={s16['n']:6d}  4h: {s16['med_pct']:+.3f}% ↑{s16['share_up']:.0%}"
                  f"   24h: {s96['med_pct']:+.3f}% ↑{s96['share_up']:.0%}")
    run_all.RESULTS.mkdir(exist_ok=True)
    (run_all.RESULTS / "continuation_study.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
