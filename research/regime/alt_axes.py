#!/usr/bin/env python3
"""Free-data axis candidates, tested on the existing harness. Encodings are PRE-REGISTERED
in NOTES.md before any result was seen — no tuning on outcome.

  A taker: taker buy/sell vol ratio 30d past-only percentile; >=0.75 votes TREND_UP,
           <=0.25 votes TREND_DOWN (replaces the OI fuel axis; reference = B2)
  B lsr:   top-trader long/short ratio, k-bar relative change with rolling-q25 deadzone;
           rising votes UP, falling votes DOWN (reference = B2)
  C dvol:  Deribit DVOL (implied vol) blended into the volatility axis as a third
           percentile; fuel stays standard OI (reference = standard F)

Metric per variant: pooled enter-TREND lead vs B1; marginal = observed raw median diff vs
the reference; paired month-cluster CI; circular time-shift null (200) of the SOURCE series.

Usage: python alt_axes.py [BTCUSDT] [--nulls 200]
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import baseline
import classifier
import features
import leadlag
import run_all

HERE = Path(__file__).resolve().parent
QW = features.DEFAULT_FP.q_window
MPF = features.DEFAULT_FP.q_min_periods_fuel
K = features.DEFAULT_FP.oi_lookback
CLASSES = ("UP", "DOWN")


def fetch_dvol(currency="BTC") -> pd.DataFrame:
    cache = HERE / "data" / f"DVOL_{currency}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    url = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    start = int(dt.datetime(2021, 3, 24, tzinfo=dt.timezone.utc).timestamp() * 1000)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    rows = []
    cur = start
    step = 1000 * 3600 * 1000  # 1000 hourly candles per call
    while cur < now:
        r = requests.get(url, params=dict(currency=currency, resolution=3600,
                                          start_timestamp=cur,
                                          end_timestamp=min(cur + step, now)), timeout=30)
        r.raise_for_status()
        rows += r.json()["result"]["data"]
        cur += step
    df = pd.DataFrame(rows, columns=["ts_start", "open", "high", "low", "dvol"])
    df = df.drop_duplicates("ts_start").sort_values("ts_start")
    # candle close value is KNOWN only at candle end: availability = start + 1h
    df["known_ts"] = pd.to_datetime(df["ts_start"].astype("int64"), unit="ms", utc=True) \
        + pd.Timedelta(hours=1)
    df["known_ts"] = df["known_ts"].astype("datetime64[ns, UTC]")
    out = df[["known_ts", "dvol"]].reset_index(drop=True)
    out.to_parquet(cache)
    return out


def variant_feat(feat_std: pd.DataFrame, source: pd.Series, kind: str) -> pd.DataFrame:
    """Build the variant feature frame from a (possibly time-shifted) source series."""
    feat = feat_std.copy()
    if kind == "taker":
        pct = features.past_pct_rank(source, QW, MPF)
        quad = np.select([pct >= 0.75, pct <= 0.25], [1, 2], 0)
        feat["quadrant"] = quad
        feat["fuel_defined"] = pct.notna().to_numpy()
    elif kind == "lsr":
        d = source / source.shift(K) - 1.0
        dz = d.abs().rolling(QW, min_periods=MPF).quantile(0.25).shift(1)
        ok = d.notna() & dz.notna()
        quad = np.select([ok & (d >= dz), ok & (d <= -dz)], [1, 2], 0)
        feat["quadrant"] = quad
        feat["fuel_defined"] = ok.to_numpy()
    elif kind == "dvol":
        dp = features.past_pct_rank(source, QW, MPF)
        feat["vol_pct"] = (feat["bbw_pct"] + feat["atrp_pct"] + dp) / 3.0
    else:
        raise ValueError(kind)
    return feat


def leads_of(B1, track, idx, exc):
    p = run_all.pair_stats(B1, track, idx, exc)
    leads = np.concatenate([p[c]["leads"] for c in CLASSES])
    months = np.concatenate([p[c]["months"] for c in CLASSES])
    churn = p["churn_fused"]
    return leads, months, churn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--nulls", type=int, default=200)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sym = args.symbol

    df, excluded, _ = run_all.load(sym)
    feat_std = features.compute(df)
    f_std = classifier.classify(feat_std)
    b2 = classifier.classify(feat_std, use_fuel=False)
    b1 = baseline.classify_b1(feat_std)
    trim = run_all.trim
    idx = trim(f_std).index
    exc = run_all.full_excluded(excluded, trim(f_std))
    B1 = trim(b1)

    lf_std, mf_std, churn_std = leads_of(B1, trim(f_std)["regime"], idx, exc)
    lb2, mb2, churn_b2 = leads_of(B1, trim(b2)["regime"], idx, exc)
    ref_med = {"B2": float(np.median(lb2)), "F": float(np.median(lf_std))}
    ref_leads = {"B2": (lb2, mb2), "F": (lf_std, mf_std)}

    # source series per variant (aligned to the candle index)
    dvol_raw = fetch_dvol("BTC" if sym.startswith("BTC") else "ETH") \
        if sym[:3] in ("BTC", "ETH") else None
    sources = {"taker": df["taker_ratio"], "lsr": df["lsr_top"]}
    if dvol_raw is not None:
        dv = pd.merge_asof(df.reset_index()[["ts"]], dvol_raw.rename(columns={"known_ts": "ts"}),
                           on="ts", direction="backward",
                           tolerance=pd.Timedelta(hours=2)).set_index("ts")["dvol"]
        sources["dvol"] = dv
    refs = {"taker": "B2", "lsr": "B2", "dvol": "F"}

    g = np.random.default_rng(77)
    n = len(df)
    out = {"symbol": sym, "variants": {}}
    for kind, src in sources.items():
        if src.isna().all():
            out["variants"][kind] = {"note": "source column empty"}
            continue
        feat_v = variant_feat(feat_std, src, kind)
        f_v = classifier.classify(feat_v)
        lv, mv, churn_v = leads_of(B1, trim(f_v)["regime"], idx, exc)
        obs_med = float(np.median(lv)) if len(lv) else np.nan
        rk = refs[kind]
        obs_marg = obs_med - ref_med[rk]
        rl, rm = ref_leads[rk]
        _, lo, hi = leadlag.paired_month_bootstrap_diff(lv, mv, rl, rm)

        nulls = []
        for i in range(args.nulls):
            s = int(g.integers(192, 96 * 90)) * (1 if g.random() < 0.5 else -1)
            src_roll = pd.Series(np.roll(src.to_numpy(), s), index=src.index)
            f_n = classifier.classify(variant_feat(feat_std, src_roll, kind))
            ln, _, _ = leads_of(B1, trim(f_n)["regime"], idx, exc)
            if len(ln):
                nulls.append(float(np.median(ln)) - ref_med[rk])
            if (i + 1) % 50 == 0:
                print(f"  {kind} null {i + 1}/{args.nulls}", flush=True)
        arr = np.array(nulls)
        p = (1 + int((arr >= obs_marg).sum())) / (len(arr) + 1) if len(arr) else None
        out["variants"][kind] = {
            "reference": rk, "n_pairs": int(len(lv)),
            "lead_median_vs_B1": obs_med, "reference_median": ref_med[rk],
            "marginal_obs": obs_marg, "marginal_ci95_month": [lo, hi],
            "null_median": float(np.median(arr)) if len(arr) else None,
            "null_iqr": [float(np.percentile(arr, 25)), float(np.percentile(arr, 75))] if len(arr) else None,
            "p_one_sided": p, "churn": round(churn_v, 3),
        }
        print(f"[{kind}] lead {obs_med} (ref {rk} {ref_med[rk]}) marginal {obs_marg:+.1f} "
              f"CI[{lo},{hi}] null_med {out['variants'][kind]['null_median']} p={p} "
              f"churn {churn_v:.2f}", flush=True)

    out["baseline_churn"] = {"F": round(churn_std, 3), "B2": round(churn_b2, 3)}
    run_all.RESULTS.mkdir(exist_ok=True)
    (run_all.RESULTS / f"{sym}_altaxes.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
