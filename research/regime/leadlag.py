#!/usr/bin/env python3
"""Lead-lag harness: EPISODE (interval) matching between label tracks.

Point-event nearest matching is gameable — a twitchy track buys "lead" with churn (multiple
flips before every slow baseline confirmation; the just-before flip gets matched). Episodes
are the honest unit: regimes are intervals, and the question is "when did the fused track's
episode corresponding to this baseline trend episode BEGIN?"

- tracks are mapped to the common 3-superstate alphabet {UP, DOWN, NT}
- trend episodes = maximal runs of UP or DOWN, length >= min_len (blips are not episodes;
  unmatched fused episodes are counted as FALSE ALARMS, a first-class metric)
- 1:1 matching per class by largest temporal overlap, overlap >= min_overlap of the shorter
  episode; no horizon parameter exists
- lead = baseline_start - fused_start (positive = fused earlier); episode ends are compared
  separately (exit timing)
- uncertainty: month-level cluster bootstrap (events are era-correlated; event-level blocks
  undercover); OI attribution uses PAIRED month resampling of (F leads, B2 leads)
- null for OI: circular time-shift of the OI series (preserves autocorrelation, breaks only
  alignment) rerun through the full fused pipeline
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

SUPER = {"TREND_UP": "UP", "TREND_DOWN": "DOWN"}


@dataclass(frozen=True)
class MatchParams:
    min_len: int = 4          # candles; sub-hour blips are noise, not episodes
    min_overlap: float = 0.25  # fraction of the shorter episode
    gap_pad: int = 8          # exclude episodes starting within +-pad of a data gap


DEFAULT_MP = MatchParams()


def to_super(labels) -> np.ndarray:
    out = np.empty(len(labels), dtype=object)
    for i, x in enumerate(labels):
        out[i] = None if pd.isna(x) else SUPER.get(x, "NT")
    return out


def episodes(sup: np.ndarray, cls: str, min_len: int) -> list:
    """Maximal runs of `cls`, length >= min_len -> [(start, end_inclusive), ...]"""
    out = []
    start = None
    for i, v in enumerate(sup):
        if v == cls and start is None:
            start = i
        elif v != cls and start is not None:
            if i - start >= min_len:
                out.append((start, i - 1))
            start = None
    if start is not None and len(sup) - start >= min_len:
        out.append((start, len(sup) - 1))
    return out


def match(base_eps: list, fused_eps: list, min_overlap: float):
    """1:1 greedy by overlap desc (deterministic tiebreak by index). Returns
    (pairs [(bi, fi, ov)], unmatched_base_idx, unmatched_fused_idx)."""
    cand = []
    for bi, (bs, be) in enumerate(base_eps):
        for fi, (fs, fe) in enumerate(fused_eps):
            ov = min(be, fe) - max(bs, fs) + 1
            if ov <= 0:
                continue
            shorter = min(be - bs, fe - fs) + 1
            if ov >= min_overlap * shorter:
                cand.append((ov, bi, fi))
    cand.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_b, used_f, pairs = set(), set(), []
    for ov, bi, fi in cand:
        if bi in used_b or fi in used_f:
            continue
        used_b.add(bi)
        used_f.add(fi)
        pairs.append((bi, fi, ov))
    un_b = [i for i in range(len(base_eps)) if i not in used_b]
    un_f = [i for i in range(len(fused_eps)) if i not in used_f]
    return pairs, un_b, un_f


def _near_gap(ep, excluded: np.ndarray, pad: int) -> bool:
    s = ep[0]
    lo, hi = max(0, s - pad), min(len(excluded), s + pad + 1)
    return bool(excluded[lo:hi].any())


def class_stats(base_sup: np.ndarray, fused_sup: np.ndarray, cls: str,
                mp: MatchParams = DEFAULT_MP, excluded: np.ndarray | None = None) -> dict:
    """Lead/miss/false-alarm stats for one episode class ('UP' or 'DOWN')."""
    n = len(base_sup)
    be = episodes(base_sup, cls, mp.min_len)
    fe = episodes(fused_sup, cls, mp.min_len)
    if excluded is not None:
        n_excl_b = sum(_near_gap(e, excluded, mp.gap_pad) for e in be)
        n_excl_f = sum(_near_gap(e, excluded, mp.gap_pad) for e in fe)
        be = [e for e in be if not _near_gap(e, excluded, mp.gap_pad)]
        fe = [e for e in fe if not _near_gap(e, excluded, mp.gap_pad)]
    else:
        n_excl_b = n_excl_f = 0
    pairs, un_b, un_f = match(be, fe, mp.min_overlap)
    leads = np.array([be[bi][0] - fe[fi][0] for bi, fi, _ in pairs])
    end_deltas = np.array([be[bi][1] - fe[fi][1] for bi, fi, _ in pairs])
    starts_b = [be[bi][0] for bi, fi, _ in pairs]
    return {
        "class": cls,
        "n_base": len(be), "n_fused": len(fe), "n_matched": len(pairs),
        "n_miss": len(un_b), "n_false": len(un_f),
        "n_excluded_base": n_excl_b, "n_excluded_fused": n_excl_f,
        "false_per_100c": len(un_f) / n * 100,
        "leads": leads, "end_deltas": end_deltas, "pair_base_starts": starts_b,
    }


def churn3(sup: np.ndarray) -> float:
    """Label flips per 100 candles on the 3-superstate alphabet (None-safe)."""
    v = [x for x in sup if x is not None]
    if len(v) < 2:
        return 0.0
    flips = sum(a != b for a, b in zip(v, v[1:]))
    return flips / len(v) * 100


def flap_rate(sup: np.ndarray) -> float:
    """Fraction of label changes reversed within 2 candles (boundary flapping)."""
    v = [x for x in sup if x is not None]
    changes = [i for i in range(1, len(v)) if v[i] != v[i - 1]]
    if not changes:
        return 0.0
    revert = sum(1 for i in changes
                 if (i + 1 < len(v) and v[i + 1] == v[i - 1])
                 or (i + 2 < len(v) and v[i + 2] == v[i - 1] and v[i + 1] == v[i]))
    return revert / len(changes)


def hodges_lehmann(x: np.ndarray) -> float:
    if len(x) == 0:
        return np.nan
    i, j = np.triu_indices(len(x))
    return float(np.median((x[i] + x[j]) / 2.0))


def month_keys(index: pd.DatetimeIndex, starts: list) -> np.ndarray:
    return np.array([f"{index[s]:%Y-%m}" for s in starts])


def month_bootstrap_median(leads: np.ndarray, months: np.ndarray,
                           n_boot: int = 2000, seed: int = 11):
    """Cluster bootstrap over calendar months: resample months, pool their leads, median."""
    if len(leads) == 0:
        return (np.nan, np.nan)
    uniq = np.unique(months)
    g = np.random.default_rng(seed)
    by_month = {m: leads[months == m] for m in uniq}
    meds = []
    for _ in range(n_boot):
        pick = g.choice(uniq, size=len(uniq), replace=True)
        pool = np.concatenate([by_month[m] for m in pick])
        meds.append(np.median(pool))
    return (float(np.percentile(meds, 2.5)), float(np.percentile(meds, 97.5)))


def paired_month_bootstrap_diff(leads_f, months_f, leads_b, months_b,
                                n_boot: int = 2000, seed: int = 11):
    """CI for median(F leads) - median(B2 leads) with the SAME month resample on both sides
    (preserves era pairing)."""
    uniq = np.unique(np.concatenate([months_f, months_b]))
    g = np.random.default_rng(seed)
    f_by = {m: leads_f[months_f == m] for m in uniq}
    b_by = {m: leads_b[months_b == m] for m in uniq}
    diffs = []
    for _ in range(n_boot):
        pick = g.choice(uniq, size=len(uniq), replace=True)
        pf = np.concatenate([f_by[m] for m in pick]) if any(len(f_by[m]) for m in pick) else np.array([])
        pb = np.concatenate([b_by[m] for m in pick]) if any(len(b_by[m]) for m in pick) else np.array([])
        if len(pf) == 0 or len(pb) == 0:
            continue
        diffs.append(np.median(pf) - np.median(pb))
    if not diffs:
        return (np.nan, np.nan, np.nan)
    return (float(np.median(diffs)), float(np.percentile(diffs, 2.5)),
            float(np.percentile(diffs, 97.5)))


def summarize(leads: np.ndarray) -> dict:
    if len(leads) == 0:
        return {"n": 0, "median": np.nan, "iqr": (np.nan, np.nan), "hl": np.nan}
    return {"n": int(len(leads)),
            "median": float(np.median(leads)),
            "iqr": (float(np.percentile(leads, 25)), float(np.percentile(leads, 75))),
            "hl": hodges_lehmann(leads)}
