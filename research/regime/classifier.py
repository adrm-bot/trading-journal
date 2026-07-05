#!/usr/bin/env python3
"""5-state regime classifier: per-axis score vectors -> weighted argmax + agreement confidence.

Output contract: one row per candle (after burn-in), {regime, confidence in [0,1], timestamp}.
No forecasts, no price targets, no trade signals. This module NEVER imports the playbook.

Design notes (adversarially reviewed, see NOTES.md):
- direction axis is agnostic among SQUEEZE/RANGE/CHOP; volatility axis is agnostic on trend —
  explicit templates keep all 5 states reachable
- fuel: only dOI-rising quadrants (new longs/new shorts) vote on the label; dOI-falling
  quadrants (short covering / long liquidation) are LABEL-INERT confidence trims, because in
  perp markets those are often the most violent trend-continuation legs
- funding is label-inert by construction (confidence-only)
- near-tie argmax prefers the incumbent state (zero-parameter anti-flap, distinct from
  min_dwell/confirm_bars which default OFF)
- hysteresis is an online FSM: a switch is stamped at the candle where it is confirmed,
  never back-dated
- degrade path: no usable OI -> fuel axis skipped, weights renormalized, flat confidence
  penalty, fuel_available flag emitted. The B2 ablation (use_fuel=False) is a legitimate
  UNPENALIZED 2-axis classifier — the penalty belongs to the production degrade path only.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

REGIMES = ("TREND_UP", "TREND_DOWN", "SQUEEZE", "RANGE", "CHOP")
IDX = {r: i for i, r in enumerate(REGIMES)}


@dataclass(frozen=True)
class ClassifierParams:
    adx_lo: float = 20.0
    adx_scale: float = 15.0
    w_dir: float = 0.5
    w_vol: float = 0.3
    w_fuel: float = 0.2
    ramp_lo: tuple = (0.15, 0.25)    # vol_pct ramp: SQUEEZE membership
    ramp_hi: tuple = (0.75, 0.85)    # vol_pct ramp: CHOP membership
    near_tie: float = 0.02
    conf_doi_down: float = 0.85      # short-covering / long-liquidation trim (label-inert)
    conf_deadzone: float = 0.95      # no-fuel deadzone trim (spec sign table)
    funding_extreme: float = 0.95    # funding percentile beyond this = crowded
    conf_funding: float = 0.90       # trim on TREND confidence when crowded
    degrade_penalty: float = 0.80    # production degrade path only
    min_dwell: int = 0
    confirm_bars: int = 0


DEFAULT_CP = ClassifierParams()


def axis_scores(feat: pd.DataFrame, p: ClassifierParams, use_fuel: bool):
    """Return (scores n x 5 normalized, valid mask, fuel_mod, fuel_votes, fuel_available)."""
    n = len(feat)
    adx = feat["adx"].to_numpy(float)
    m = feat["m"].to_numpy(float)
    di = feat["di_diff"].to_numpy(float)
    v = feat["vol_pct"].to_numpy(float)

    valid = ~(np.isnan(adx) | np.isnan(m) | np.isnan(v))

    # --- direction axis ---
    a = np.clip((adx - p.adx_lo) / p.adx_scale, 0.0, 1.0)
    T = a * np.tanh(np.abs(m))
    agree = np.sign(di) == np.sign(m)
    T = np.where(agree, T, T * 0.5)
    dir_s = np.zeros((n, 5))
    dir_s[:, IDX["TREND_UP"]] = np.where(m > 0, T, 0.0)
    dir_s[:, IDX["TREND_DOWN"]] = np.where(m < 0, T, 0.0)
    rest = (1.0 - dir_s[:, 0] - dir_s[:, 1]) / 3.0
    for s in ("SQUEEZE", "RANGE", "CHOP"):
        dir_s[:, IDX[s]] = rest

    # --- volatility axis ---
    lo0, lo1 = p.ramp_lo
    hi0, hi1 = p.ramp_hi
    muL = np.clip((lo1 - v) / (lo1 - lo0), 0.0, 1.0)
    muH = np.clip((v - hi0) / (hi1 - hi0), 0.0, 1.0)
    muM = np.clip(1.0 - muL - muH, 0.0, 1.0)
    vol_s = np.zeros((n, 5))
    vol_s[:, IDX["SQUEEZE"]] = muL
    vol_s[:, IDX["RANGE"]] = muM
    vol_s[:, IDX["CHOP"]] = muH

    # --- fuel axis ---
    fuel_votes = np.zeros(n, dtype=bool)
    fuel_mod = np.ones(n)
    fuel_available = np.zeros(n, dtype=bool)
    fuel_s = np.zeros((n, 5))
    if use_fuel and "quadrant" in feat.columns:
        quad = feat["quadrant"].to_numpy()
        fuel_available = feat["fuel_defined"].to_numpy(bool)
        fuel_votes = fuel_available & ((quad == 1) | (quad == 2))
        fuel_s[quad == 1, IDX["TREND_UP"]] = 1.0
        fuel_s[quad == 2, IDX["TREND_DOWN"]] = 1.0
        fuel_mod = np.where(fuel_available & ((quad == 3) | (quad == 4)),
                            p.conf_doi_down, fuel_mod)
        fuel_mod = np.where(fuel_available & (quad == 0), p.conf_deadzone, fuel_mod)

    w_present = p.w_dir + p.w_vol + np.where(fuel_votes, p.w_fuel, 0.0)
    scores = (p.w_dir * dir_s + p.w_vol * vol_s
              + p.w_fuel * fuel_s * fuel_votes[:, None]) / w_present[:, None]
    return scores, valid, fuel_mod, fuel_votes, fuel_available


def classify(feat: pd.DataFrame, p: ClassifierParams = DEFAULT_CP, *,
             use_fuel: bool = True, use_funding: bool = True,
             debug: bool = False) -> pd.DataFrame:
    """feat: features.compute() output. Returns DataFrame(regime, confidence) on feat.index.

    Rows where core features are NaN (burn-in) get regime=None, confidence=NaN.
    Degrade path is PER-ROW and causal: any row without usable fuel (missing OI column,
    stale OI, deadzone-window warmup) gets the flat degrade penalty on confidence — no
    whole-series look-ahead decides the mode. The B2 ablation (use_fuel=False) stays
    unpenalized by definition.
    """
    scores, valid, fuel_mod, fuel_votes, fuel_available = axis_scores(feat, p, use_fuel)
    n = len(feat)
    fund = feat["funding_pct"].to_numpy(float) if "funding_pct" in feat.columns \
        else np.full(n, np.nan)

    labels = np.full(n, -1, dtype=np.int8)
    conf = np.full(n, np.nan)

    cur = -1
    pending, pend_count, since_switch = -1, 0, 10**9
    best_arr = np.argmax(scores, axis=1)  # first-max on ties (matches the Pine port)
    for t in range(n):
        if not valid[t]:
            cur, pending, pend_count, since_switch = -1, -1, 0, 10**9
            continue
        best = best_arr[t]
        # near-tie: prefer the incumbent
        cand = cur if (cur >= 0 and scores[t, cur] >= scores[t, best] - p.near_tie) else best
        if cur < 0:
            cur, since_switch = cand, 0
        elif cand != cur:
            if cand == pending:
                pend_count += 1
            else:
                pending, pend_count = cand, 1
            if pend_count >= p.confirm_bars + 1 and since_switch >= p.min_dwell:
                cur, since_switch = cand, 0
                pending, pend_count = -1, 0
        else:
            pending, pend_count = -1, 0
        since_switch += 1
        labels[t] = cur

        agreement = scores[t, cur]
        runner = max(scores[t, s] for s in range(5) if s != cur)
        margin = 1.0 - 0.5 * (runner / agreement if agreement > 1e-12 else 2.0)
        c = agreement * margin * fuel_mod[t]
        if use_funding and not np.isnan(fund[t]) and cur in (0, 1):
            if fund[t] >= p.funding_extreme or fund[t] <= 1.0 - p.funding_extreme:
                c *= p.conf_funding
        if use_fuel and not fuel_available[t]:
            c *= p.degrade_penalty  # per-row causal degrade (fuel wanted but unusable here)
        conf[t] = min(max(c, 0.0), 1.0)

    regime = pd.array([REGIMES[i] if i >= 0 else None for i in labels])
    out = pd.DataFrame({"regime": regime, "confidence": conf}, index=feat.index)
    out["fuel_available"] = fuel_available if use_fuel else False
    if debug:
        for i, r in enumerate(REGIMES):
            out[f"score_{r}"] = scores[:, i]
        out["fuel_mod"] = fuel_mod
        out["fuel_votes"] = fuel_votes
    return out


def to_records(res: pd.DataFrame) -> list:
    """Output contract: [{regime, confidence, timestamp}] for classified candles only."""
    ok = res["regime"].notna()
    return [{"regime": r, "confidence": float(c), "timestamp": ts.isoformat()}
            for ts, r, c in zip(res.index[ok], res.loc[ok, "regime"], res.loc[ok, "confidence"])]
