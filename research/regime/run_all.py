#!/usr/bin/env python3
"""End-to-end research runner.

Stages (each writes results/<SYMBOL>_<stage>.json and prints a summary):
  headline  - lead-lag tables: F vs B1, B2 vs B1, OI marginal contribution (the ONE number),
              churn/flap/false-alarm columns, era decomposition, descriptives,
              sensitivity grid (k x deadzone; d x overlap), OI-lag robustness
  null      - circular time-shift null for the OI marginal (200 shifts) -> permutation p
  collapse  - confidence-collapse early-warning angle vs B1 transitions, time-shift null,
              funding ablation
  sweep     - hysteresis grid (min_dwell x confirm_bars) vs lead/churn table

Usage: python run_all.py BTCUSDT [--stage headline|null|collapse|sweep|all] [--nulls 200]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import baseline
import classifier
import features
import leadlag
from classifier import ClassifierParams
from features import FeatureParams
from leadlag import MatchParams

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
BURN = features.DEFAULT_FP.burn_in
ERAS = [("2022_bear", "2022-01-01", "2022-12-31"),
        ("2023_range", "2023-01-01", "2023-12-31"),
        ("2024_25_bull", "2024-01-01", "2025-12-31"),
        ("2026", "2026-01-01", "2026-12-31")]
CLASSES = ("UP", "DOWN")


def load(symbol: str):
    df = pd.read_parquet(HERE / "data" / f"{symbol}_15m.parquet")
    gaps = json.loads((HERE / "data" / f"{symbol}_gaps.json").read_text())
    excluded = (df["bar_missing"] | ~df["oi_fresh"]).to_numpy(bool)
    return df, excluded, gaps


def tracks(df, fp: FeatureParams = features.DEFAULT_FP, cp: ClassifierParams = classifier.DEFAULT_CP,
           oi_col: str = "oi"):
    feat = features.compute(df, fp, oi_col=oi_col)
    f = classifier.classify(feat, cp)
    b2 = classifier.classify(feat, cp, use_fuel=False)
    b1 = baseline.classify_b1(feat)
    return feat, f, b2, b1


def trim(x, burn=BURN):
    return x.iloc[burn:]


def full_excluded(excluded: np.ndarray, f_trimmed: pd.DataFrame) -> np.ndarray:
    """Data gaps PLUS the fused track's post-gap blackout (rows it could not classify):
    the FSM cold-restarts after a gap, and B1 keeps labeling straight through — episodes
    born inside that blackout would contaminate miss counts and lead outliers. Applied
    symmetrically to every track pairing."""
    return excluded[BURN:] | pd.isna(f_trimmed["regime"].to_numpy(object))


def pair_stats(base_lab, fused_lab, index, excluded, mp=MatchParams()):
    """Per-class episode stats + month keys for bootstrap."""
    bs = leadlag.to_super(base_lab)
    fs = leadlag.to_super(fused_lab)
    out = {}
    for cls in CLASSES:
        st = leadlag.class_stats(bs, fs, cls, mp, excluded)
        st["months"] = leadlag.month_keys(index, st["pair_base_starts"])
        out[cls] = st
    out["churn_base"] = leadlag.churn3(bs)
    out["churn_fused"] = leadlag.churn3(fs)
    out["flap_fused"] = leadlag.flap_rate(fs)
    return out


def _cls_row(st):
    s = leadlag.summarize(st["leads"])
    ci = leadlag.month_bootstrap_median(st["leads"], st["months"])
    ends = leadlag.summarize(st["end_deltas"])
    ratio = st["n_fused"] / st["n_base"] if st["n_base"] else np.nan
    return {
        "n_base_eps": st["n_base"], "n_fused_eps": st["n_fused"],
        "eps_ratio": round(ratio, 3), "ratio_invalid": bool(ratio > 1.5),
        "matched_frac": round(st["n_matched"] / st["n_base"], 3) if st["n_base"] else np.nan,
        "n_miss": st["n_miss"], "n_false": st["n_false"],
        "false_per_100c": round(st["false_per_100c"], 3),
        "start_lead": {**s, "ci95_month_boot": ci},
        "end_lead": ends,
        "excluded": [st["n_excluded_base"], st["n_excluded_fused"]],
    }


def headline(symbol: str, write=True):
    df, excluded, _ = load(symbol)
    feat, f, b2, b1 = tracks(df)
    idx = trim(f).index
    exc = full_excluded(excluded, trim(f))
    F, B2, B1 = trim(f)["regime"], trim(b2)["regime"], trim(b1)

    fvb = pair_stats(B1, F, idx, exc)
    avb = pair_stats(B1, B2, idx, exc)

    out = {"symbol": symbol, "n_candles": len(idx),
           "span": [str(idx[0]), str(idx[-1])],
           "F_vs_B1": {}, "B2_vs_B1": {}, "OI_marginal": {}}
    # point estimate = OBSERVED raw median difference (the same estimand the null stage
    # tests); the paired month bootstrap contributes the CI only (its own median is kept as
    # a diagnostic — mixing the two up would report a smoothed number against a raw null)
    for cls in CLASSES:
        out["F_vs_B1"][cls] = _cls_row(fvb[cls])
        out["B2_vs_B1"][cls] = _cls_row(avb[cls])
        obs_diff = float(np.median(fvb[cls]["leads"]) - np.median(avb[cls]["leads"])) \
            if len(fvb[cls]["leads"]) and len(avb[cls]["leads"]) else np.nan
        boot_med, lo, hi = leadlag.paired_month_bootstrap_diff(
            fvb[cls]["leads"], fvb[cls]["months"], avb[cls]["leads"], avb[cls]["months"])
        out["OI_marginal"][cls] = {"median_diff": obs_diff, "ci95": [lo, hi],
                                   "boot_median_diff": boot_med}
    # pooled enter-TREND (both directions) — the pre-registered primary endpoint uses this
    lf = np.concatenate([fvb[c]["leads"] for c in CLASSES])
    mf = np.concatenate([fvb[c]["months"] for c in CLASSES])
    lb = np.concatenate([avb[c]["leads"] for c in CLASSES])
    mb = np.concatenate([avb[c]["months"] for c in CLASSES])
    boot_med, lo, hi = leadlag.paired_month_bootstrap_diff(lf, mf, lb, mb)
    obs_pooled = float(np.median(lf) - np.median(lb)) if len(lf) and len(lb) else np.nan
    out["OI_marginal"]["enter_pooled"] = {"median_diff": obs_pooled, "ci95": [lo, hi],
                                          "boot_median_diff": boot_med,
                                          "F_median": float(np.median(lf)) if len(lf) else np.nan,
                                          "B2_median": float(np.median(lb)) if len(lb) else np.nan}
    out["churn"] = {"B1": round(fvb["churn_base"], 3), "F": round(fvb["churn_fused"], 3),
                    "B2": round(avb["churn_fused"], 3), "F_flap": round(fvb["flap_fused"], 3)}

    # era decomposition (sign consistency): median F-vs-B1 lead per era, per class pooled
    eras = {}
    for name, a, b in ERAS:
        t0 = pd.Timestamp(a, tz="UTC")
        t1 = pd.Timestamp(b, tz="UTC") + pd.Timedelta(days=1)
        leads = []
        for cls in CLASSES:
            st = fvb[cls]
            sel = [(idx[s] >= t0) and (idx[s] < t1) for s in st["pair_base_starts"]]
            leads += list(st["leads"][np.array(sel, dtype=bool)]) if st["n_matched"] else []
        eras[name] = {"n": len(leads),
                      "median": float(np.median(leads)) if leads else np.nan}
    out["eras"] = eras
    signs = [e["median"] for e in eras.values() if e["n"] >= 10]
    out["era_sign_consistency"] = f"{sum(1 for s in signs if s > 0)}/{len(signs)} eras positive"

    # descriptives: occupancy + dwell per state (the current-state deliverable's health check)
    occ = F.value_counts(normalize=True).round(4).to_dict()
    sup = leadlag.to_super(F)
    dwell = {}
    for state in classifier.REGIMES:
        runs = []
        cnt = 0
        for x in F:
            if (not pd.isna(x)) and x == state:
                cnt += 1
            elif cnt:
                runs.append(cnt)
                cnt = 0
        if cnt:
            runs.append(cnt)
        dwell[state] = {"n_runs": len(runs), "median_bars": float(np.median(runs)) if runs else 0}
    out["descriptives"] = {"occupancy": occ, "dwell": dwell,
                           "native_churn_F": round(float((F != F.shift()).sum() / len(F) * 100), 3)}

    # sensitivity: fuel params (k x deadzone) -> pooled enter median of F vs B1
    sens = {}
    for k in (4, 8, 16):
        for dq in (0.10, 0.25):
            fp2 = FeatureParams(oi_lookback=k, dead_q=dq)
            _, f2, _, _ = tracks(df, fp=fp2)
            p2 = pair_stats(B1, trim(f2)["regime"], idx, exc)
            leads2 = np.concatenate([p2[c]["leads"] for c in CLASSES])
            sens[f"k{k}_dz{dq}"] = {"median": float(np.median(leads2)) if len(leads2) else np.nan,
                                    "n": int(len(leads2)),
                                    "churn_F": round(p2["churn_fused"], 3)}
    out["sensitivity_fuel"] = sens
    # sensitivity: matching params (min_len x overlap)
    sens_m = {}
    for d in (1, 4, 8):
        for ovl in (0.01, 0.25, 0.5):
            mp = MatchParams(min_len=d, min_overlap=ovl)
            p2 = pair_stats(B1, F, idx, exc, mp)
            leads2 = np.concatenate([p2[c]["leads"] for c in CLASSES])
            sens_m[f"d{d}_ov{ovl}"] = {"median": float(np.median(leads2)) if len(leads2) else np.nan,
                                       "n": int(len(leads2))}
    out["sensitivity_match"] = sens_m

    # OI publication-latency robustness: fuel from oi lagged one 5m snapshot
    if "oi_lag1" in df.columns:
        _, f_lag, _, _ = tracks(df, oi_col="oi_lag1")
        pl = pair_stats(B1, trim(f_lag)["regime"], idx, exc)
        leads_l = np.concatenate([pl[c]["leads"] for c in CLASSES])
        med_diff_l, lo_l, hi_l = leadlag.paired_month_bootstrap_diff(
            leads_l, np.concatenate([pl[c]["months"] for c in CLASSES]), lb, mb)
        out["oi_lag1_robustness"] = {
            "F_median": float(np.median(leads_l)) if len(leads_l) else np.nan,
            "OI_marginal_median_diff": med_diff_l, "ci95": [lo_l, hi_l]}

    if write:
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / f"{symbol}_headline.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def null_run(symbol: str, n_shifts=200, seed=42):
    """Circular time-shift null: roll the OI series (alignment broken, autocorrelation kept),
    rerun the FULL fused pipeline, pooled enter median lead vs B1 per shift.
    p = P(null OI-marginal >= observed OI-marginal)."""
    df, excluded, _ = load(symbol)
    feat, f, b2, b1 = tracks(df)
    idx = trim(f).index
    exc = full_excluded(excluded, trim(f))
    B1 = trim(b1)
    obs = pair_stats(B1, trim(f)["regime"], idx, exc)
    obs_med = float(np.median(np.concatenate([obs[c]["leads"] for c in CLASSES])))
    b2s = pair_stats(B1, trim(b2)["regime"], idx, exc)
    b2_med = float(np.median(np.concatenate([b2s[c]["leads"] for c in CLASSES])))
    obs_marginal = obs_med - b2_med

    obs_cls = {c: float(np.median(obs[c]["leads"])) - float(np.median(b2s[c]["leads"]))
               for c in CLASSES}

    g = np.random.default_rng(seed)
    n = len(df)
    lo, hi = 192, 96 * 90  # 2 days .. 90 days
    null_marginals = []
    null_cls = {c: [] for c in CLASSES}
    oi = df["oi"].to_numpy()
    fresh = df["oi_fresh"].to_numpy()
    bar_missing = df["bar_missing"].to_numpy(bool)
    k_lb = features.DEFAULT_FP.oi_lookback
    for i in range(n_shifts):
        s = int(g.integers(lo, hi)) * (1 if g.random() < 0.5 else -1)
        df2 = df.copy()
        df2["oi"] = np.roll(oi, s)
        fresh2 = np.roll(fresh, s).copy()
        # mask the wrap seam: dOI across it is a fake discontinuity, not misalignment
        seam = s % n
        fresh2[max(0, seam - k_lb):min(n, seam + k_lb + 1)] = False
        df2["oi_fresh"] = fresh2
        feat2 = features.compute(df2)
        f2 = classifier.classify(feat2)
        # exclusion mask must follow the ROLLED freshness (same filtering rule as observed)
        exc2 = (bar_missing | ~fresh2)[BURN:] | pd.isna(trim(f2)["regime"].to_numpy(object))
        p2 = pair_stats(B1, trim(f2)["regime"], idx, exc2)
        leads2 = np.concatenate([p2[c]["leads"] for c in CLASSES])
        if len(leads2):
            null_marginals.append(float(np.median(leads2)) - b2_med)
        for c in CLASSES:
            if len(p2[c]["leads"]):
                null_cls[c].append(float(np.median(p2[c]["leads"]))
                                   - float(np.median(b2s[c]["leads"])))
        if (i + 1) % 20 == 0:
            print(f"  null {i + 1}/{n_shifts}", flush=True)
    null_arr = np.array(null_marginals)
    p = (1 + int((null_arr >= obs_marginal).sum())) / (len(null_arr) + 1)
    per_class = {}
    for c in CLASSES:
        arr = np.array(null_cls[c])
        if len(arr) == 0:
            per_class[c] = {"observed": obs_cls[c], "n_null": 0, "p_one_sided": None,
                            "note": "degenerate null — no replicates produced leads"}
            continue
        per_class[c] = {"observed": obs_cls[c], "n_null": int(len(arr)),
                        "null_median": float(np.median(arr)),
                        "null_iqr": [float(np.percentile(arr, 25)), float(np.percentile(arr, 75))],
                        "p_one_sided": (1 + int((arr >= obs_cls[c]).sum())) / (len(arr) + 1)}
    out = {"symbol": symbol, "observed_F_median": obs_med, "B2_median": b2_med,
           "observed_OI_marginal": obs_marginal,
           "null_n": len(null_arr), "null_median": float(np.median(null_arr)),
           "null_iqr": [float(np.percentile(null_arr, 25)), float(np.percentile(null_arr, 75))],
           "p_one_sided": p, "per_class": per_class}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{symbol}_null.json").write_text(json.dumps(out, indent=1))
    return out


def collapse_events(conf: pd.Series, fuel_avail: pd.Series, excluded: np.ndarray,
                    q_window=2880, debounce=2, pad=96):
    """Collapse = confidence crossing below its own past-only rolling q20, needing `debounce`
    consecutive bars; the event is stamped at the bar where the criterion is SATISFIED.
    Events near data gaps or fuel-availability boundaries are masked."""
    thr = conf.rolling(q_window, min_periods=q_window).quantile(0.2).shift(1)
    below = (conf < thr).to_numpy(bool)
    ok = np.zeros(len(conf), dtype=bool)
    run = 0
    for i, b in enumerate(below):
        run = run + 1 if b else 0
        if run == debounce:  # stamped at satisfaction bar, never back-dated
            ok[i] = True
    # mask around gaps and availability flips
    bad = excluded.copy()
    av = fuel_avail.to_numpy(bool)
    flips = np.flatnonzero(av[1:] != av[:-1]) + 1
    for i in np.concatenate([flips, np.flatnonzero(excluded)]):
        bad[max(0, i - pad):i + pad] = True
    ok &= ~bad
    return np.flatnonzero(ok)


def collapse_metrics(ev_idx: np.ndarray, trans_idx: np.ndarray, n: int, W=96):
    """Convention: a collapse stamped exactly ON a transition bar counts as a lead-0 hit,
    on BOTH sides (precision via side='left' on transitions, recall via side='right' on
    events — asymmetric sides would silently deflate recall for coincident events)."""
    if len(ev_idx) == 0 or len(trans_idx) == 0:
        return {"n_events": int(len(ev_idx)), "n_transitions": int(len(trans_idx)),
                "precision": np.nan, "recall": np.nan, "median_lead": np.nan}
    t = np.sort(trans_idx)
    pos = np.searchsorted(t, ev_idx, side="left")
    dist = np.where(pos < len(t), t[np.minimum(pos, len(t) - 1)] - ev_idx, n)
    hits = dist <= W
    e = np.sort(ev_idx)
    pos_e = np.searchsorted(e, t, side="right")
    prev_ev = np.where(pos_e > 0, t - e[np.maximum(pos_e - 1, 0)], n)
    covered = prev_ev <= W
    return {"n_events": int(len(ev_idx)), "n_transitions": int(len(t)),
            "precision": float(hits.mean()),
            "recall": float(covered.mean()),
            "median_lead": float(np.median(dist[hits])) if hits.any() else np.nan}


def collapse_stage(symbol: str, n_shifts=200, seed=43, W=96):
    df, excluded, _ = load(symbol)
    feat, f, b2, b1 = tracks(df)
    conf = trim(f)["confidence"]
    fuel_avail = trim(f)["fuel_available"]
    exc = full_excluded(excluded, trim(f))
    B1 = trim(b1)
    lab = B1.to_numpy(object)
    valid = ~pd.isna(lab)
    trans = np.flatnonzero(valid[1:] & valid[:-1] & (lab[1:] != lab[:-1])) + 1
    n = len(conf)

    ev = collapse_events(conf, fuel_avail, exc)
    obs = collapse_metrics(ev, trans, n, W)
    if len(ev) == 0 or np.isnan(obs["precision"]):
        out = {"symbol": symbol, "observed": obs, "p_precision": None, "p_recall": None,
               "note": "no collapse events — p undefined"}
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / f"{symbol}_collapse.json").write_text(json.dumps(out, indent=1))
        return out

    g = np.random.default_rng(seed)
    null_prec, null_lead, null_rec = [], [], []
    for _ in range(n_shifts):
        s = int(g.integers(W, n - W))
        ev_s = (ev + s) % n
        m = collapse_metrics(np.sort(ev_s), trans, n, W)
        null_prec.append(m["precision"])
        null_rec.append(m["recall"])
        if not np.isnan(m["median_lead"]):
            null_lead.append(m["median_lead"])
    p_prec = (1 + sum(1 for x in null_prec if x >= obs["precision"])) / (n_shifts + 1)
    p_rec = (1 + sum(1 for x in null_rec if x >= obs["recall"])) / (n_shifts + 1)

    # funding ablation: same angle with the funding modifier off
    f_nf = classifier.classify(feat, use_funding=False)
    ev_nf = collapse_events(trim(f_nf)["confidence"], trim(f_nf)["fuel_available"], exc)
    obs_nf = collapse_metrics(ev_nf, trans, n, W)

    out = {"symbol": symbol, "window_bars": W, "observed": obs,
           "null": {"precision_mean": float(np.mean(null_prec)),
                    "recall_mean": float(np.mean(null_rec)),
                    "median_lead_mean": float(np.mean(null_lead)) if null_lead else np.nan},
           "p_precision": p_prec, "p_recall": p_rec,
           "funding_ablation": obs_nf}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{symbol}_collapse.json").write_text(json.dumps(out, indent=1))
    return out


def posthoc(symbol: str, horizons=(4, 16, 96)):
    """Post-hoc label audit: was each state RIGHT about the market's character?
    For every candle labeled state S, the realized return over the NEXT h bars.
    Uses future data BY DESIGN — this is a retrospective audit of past labels
    (published as diagnostics), never a feature; the classifier contract is untouched."""
    df, excluded, _ = load(symbol)
    feat, f, b2, b1 = tracks(df)
    F = trim(f)["regime"]
    exc = full_excluded(excluded, trim(f))
    lab = F.to_numpy(object)
    c = df["close"].iloc[BURN:].to_numpy(float)
    out = {"symbol": symbol, "horizons_bars": list(horizons), "states": {}}
    for state in classifier.REGIMES:
        base = (~pd.isna(lab)) & (lab == state) & ~exc
        row = {}
        for h in horizons:
            mask = base.copy()
            mask[-h:] = False
            r = (np.roll(c, -h) / c - 1.0)[mask]  # wrapped tail rows are masked off
            row[str(h)] = {
                "n": int(mask.sum()),
                "median_ret_pct": round(float(np.median(r)) * 100, 3),
                "share_up": round(float((r > 0).mean()), 3),
                "iqr_pct": [round(float(np.percentile(r, 25)) * 100, 3),
                            round(float(np.percentile(r, 75)) * 100, 3)],
                "abs_move_med_pct": round(float(np.median(np.abs(r))) * 100, 3),
            }
        out["states"][state] = row
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{symbol}_posthoc.json").write_text(json.dumps(out, indent=1))

    names = {"TREND_UP": "상승추세", "TREND_DOWN": "하락추세", "SQUEEZE": "수렴",
             "RANGE": "박스권", "CHOP": "난장판"}
    hours = {4: "1시간", 16: "4시간", 96: "24시간"}
    print(f"\n[{symbol}] 상태별 사후 성과 — '라벨이 붙은 뒤 실제로 어떻게 갔나'")
    print(f"{'상태':12s}" + "".join(f"{hours.get(h, str(h)+'봉'):>26s}" for h in horizons))
    for state in classifier.REGIMES:
        cells = []
        for h in horizons:
            d = out["states"][state][str(h)]
            cells.append(f"중앙 {d['median_ret_pct']:+.2f}% ↑비율 {d['share_up']:.0%} "
                         f"|폭| {d['abs_move_med_pct']:.2f}%")
        print(f"{names[state]:12s}" + "".join(f"{s:>26s}" for s in cells))
    return out


def sweep(symbol: str):
    df, excluded, _ = load(symbol)
    feat, f, b2, b1 = tracks(df)
    idx = trim(f).index
    exc = full_excluded(excluded, trim(f))
    B1 = trim(b1)
    base_pooled = None
    rows = {}
    for dwell in (0, 2, 4, 8):
        for confirm in (0, 1, 2, 3):
            cp = ClassifierParams(min_dwell=dwell, confirm_bars=confirm)
            f2 = classifier.classify(feat, cp)
            p2 = pair_stats(B1, trim(f2)["regime"], idx, exc)
            leads2 = np.concatenate([p2[c]["leads"] for c in CLASSES])
            med = float(np.median(leads2)) if len(leads2) else np.nan
            if dwell == 0 and confirm == 0:
                base_pooled = med
            rows[f"dwell{dwell}_confirm{confirm}"] = {
                "median_lead": med, "n": int(len(leads2)),
                "lead_sacrificed": round(base_pooled - med, 2) if base_pooled is not None else np.nan,
                "churn3_F": round(p2["churn_fused"], 3),
                "flap_F": round(p2["flap_fused"], 3),
                "false_per_100c": round(sum(p2[c]["false_per_100c"] for c in CLASSES), 3),
            }
    out = {"symbol": symbol, "grid": rows,
           "churn3_B1": round(leadlag.churn3(leadlag.to_super(B1)), 3)}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{symbol}_hysteresis.json").write_text(json.dumps(out, indent=1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--stage", default="headline")
    ap.add_argument("--nulls", type=int, default=200)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    stages = ["headline", "null", "collapse", "sweep", "posthoc"] if args.stage == "all" \
        else [args.stage]
    for st in stages:
        print(f"=== {args.symbol} :: {st} ===", flush=True)
        if st == "headline":
            out = headline(args.symbol)
            print(json.dumps({k: out[k] for k in
                              ["F_vs_B1", "B2_vs_B1", "OI_marginal", "churn", "eras",
                               "era_sign_consistency"]}, indent=1, default=str))
        elif st == "null":
            print(json.dumps(null_run(args.symbol, args.nulls), indent=1))
        elif st == "collapse":
            print(json.dumps(collapse_stage(args.symbol, args.nulls), indent=1))
        elif st == "sweep":
            print(json.dumps(sweep(args.symbol), indent=1))
        elif st == "posthoc":
            posthoc(args.symbol)


if __name__ == "__main__":
    main()
