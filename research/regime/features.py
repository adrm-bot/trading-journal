#!/usr/bin/env python3
"""Causal feature layer.

Every value at row t uses data from candle closes <= t (the current candle's own close is
included by convention — labels are stamped at close time). This is the only module that
computes indicators; classifier/baseline consume the returned frame and never touch raw data.

Causality rules enforced here:
- rolling windows end at the current row (pandas default); никогда center=True
- percentile ranks are computed against the STRICTLY-PAST window: from the inclusive rolling
  rank r over w obs, past-only pct = (r*w - 1)/(w - 1)  (removes the current value's self-count)
- threshold-style quantiles use .shift(1)
- recursive filters (EMA/Wilder) run forward from the first row only
- dOI is defined only when BOTH endpoints have fresh OI
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

BARS_PER_DAY = 96


@dataclass(frozen=True)
class FeatureParams:
    adx_n: int = 14
    ema_fast: int = 20
    ema_slow: int = 50
    atr_n: int = 14
    bb_n: int = 20
    bb_k: float = 2.0
    q_window: int = 30 * BARS_PER_DAY          # 30d rolling window for percentiles
    q_min_periods_fuel: int = 20 * BARS_PER_DAY  # fuel deadzone tolerates shorter history (live mode)
    oi_lookback: int = 8                        # dOI / dprice lookback, candles
    dead_q: float = 0.25                        # deadzone quantile for |dOI%| and |dprice|
    burn_in: int = 35 * BARS_PER_DAY


DEFAULT_FP = FeatureParams()

V2_SUPPORTED_CHART_MINUTES = (5, 15, 60, 240)
V2_CORE_MINUTES_BY_CHART = {5: 15, 15: 15, 60: 15, 240: 15}


def v2_params_for_chart(chart_minutes: int) -> FeatureParams:
    """Return the wall-clock-equivalent v2 feature window for a supported chart.

    Every supported chart consumes the canonical 15m core.  This avoids promoting native
    5m/1h/4h variants that have not passed the four-asset acceptance gates.  The chart is a
    display surface; it does not retune the classifier.
    """
    if chart_minutes not in V2_CORE_MINUTES_BY_CHART:
        raise ValueError(
            f"unsupported v2 chart timeframe {chart_minutes}m; "
            f"expected one of {V2_SUPPORTED_CHART_MINUTES}"
        )
    core_minutes = V2_CORE_MINUTES_BY_CHART[chart_minutes]
    bars_per_day = 1440 // core_minutes
    return FeatureParams(
        q_window=30 * bars_per_day,
        q_min_periods_fuel=20 * bars_per_day,
        oi_lookback=max(1, 120 // core_minutes),
        burn_in=35 * bars_per_day,
    )


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing (RMA) = EMA with alpha=1/n, forward-recursive (causal)."""
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def adx_di(h: pd.Series, l: pd.Series, c: pd.Series, n: int):
    up = h.diff()
    dn = -l.diff()
    # NaN inputs (missing bars) must stay NaN — NaN>NaN is False, which would otherwise
    # inject fake zero-directional-movement observations into the Wilder recursion
    ok = up.notna() & dn.notna()
    pdm = up.where((up > dn) & (up > 0), 0.0).where(ok)
    ndm = dn.where((dn > up) & (dn > 0), 0.0).where(ok)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = wilder(tr, n)
    pdi = 100 * wilder(pdm, n) / atr
    ndi = 100 * wilder(ndm, n) / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    adx = wilder(dx, n)
    return adx, pdi, ndi, atr


def past_pct_rank(s: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Percentile of the current value against the strictly-past part of the rolling window."""
    r = s.rolling(window, min_periods=min_periods).rank(pct=True)
    cnt = s.rolling(window, min_periods=min_periods).count()
    return ((r * cnt - 1) / (cnt - 1)).where(cnt > 1)


def pine_percentrank(s: pd.Series, window: int) -> pd.Series:
    """Exact continuous-grid equivalent of Pine ``ta.percentrank(source, window)``.

    Pine compares the current value with the *previous* ``window`` values and counts prior
    values less than or equal to it.  Rolling ``rank(method='max')`` over current+prior rows
    counts the current observation too, so subtracting one reproduces Pine, including ties.
    """
    rank_including_current = s.rolling(
        window + 1, min_periods=window + 1
    ).rank(method="max")
    return (rank_including_current - 1.0) / window


def compute_v2(df: pd.DataFrame, fp: FeatureParams = DEFAULT_FP) -> pd.DataFrame:
    """Price-only v2 feature stream with TradingView-percent-rank semantics.

    This leaves ``compute()`` and every v1 research default untouched.  It deliberately
    omits OI, funding, dead-zone quantiles, and fuel columns, and is the canonical feature
    producer for ``classify_v2()`` plus Python/Pine differential exports.
    """
    out = pd.DataFrame(index=df.index)
    c, h, l = df["close"], df["high"], df["low"]
    adx, pdi, ndi, atr = adx_di(h, l, c, fp.adx_n)
    ema_f = c.ewm(span=fp.ema_fast, adjust=False, min_periods=fp.ema_fast).mean()
    ema_s = c.ewm(span=fp.ema_slow, adjust=False, min_periods=fp.ema_slow).mean()
    out["adx"] = adx
    out["di_diff"] = pdi - ndi
    out["m"] = (ema_f - ema_s) / atr.replace(0, np.nan)

    mid = c.rolling(fp.bb_n, min_periods=fp.bb_n).mean()
    sd = c.rolling(fp.bb_n, min_periods=fp.bb_n).std()
    bbw = (2 * fp.bb_k * sd) / mid.replace(0, np.nan)
    atrp = atr / c
    out["bbw_pct"] = pine_percentrank(bbw, fp.q_window)
    out["atrp_pct"] = pine_percentrank(atrp, fp.q_window)
    out["vol_pct"] = 0.5 * out["bbw_pct"] + 0.5 * out["atrp_pct"]
    out["close"] = c
    out["bar_missing"] = df["bar_missing"] if "bar_missing" in df.columns else False
    return out


def compute(df: pd.DataFrame, fp: FeatureParams = DEFAULT_FP, oi_col: str = "oi") -> pd.DataFrame:
    """df: build_dataset output (index=close ts). Returns feature frame on the same index."""
    out = pd.DataFrame(index=df.index)
    c, h, l = df["close"], df["high"], df["low"]

    adx, pdi, ndi, atr = adx_di(h, l, c, fp.adx_n)
    ema_f = c.ewm(span=fp.ema_fast, adjust=False, min_periods=fp.ema_fast).mean()
    ema_s = c.ewm(span=fp.ema_slow, adjust=False, min_periods=fp.ema_slow).mean()
    out["adx"] = adx
    out["di_diff"] = pdi - ndi
    out["m"] = (ema_f - ema_s) / atr.replace(0, np.nan)

    # volatility percentile blend (strictly-past distributions). min_periods gets 5% slack:
    # with min_periods == window, ONE missing bar blanks vol_pct for a full window length
    # (30 days) — the classifier then cold-resets for that whole stretch
    mp_vol = max(2, int(fp.q_window * 0.95))
    mid = c.rolling(fp.bb_n, min_periods=fp.bb_n).mean()
    sd = c.rolling(fp.bb_n, min_periods=fp.bb_n).std()
    bbw = (2 * fp.bb_k * sd) / mid.replace(0, np.nan)
    atrp = atr / c
    out["bbw_pct"] = past_pct_rank(bbw, fp.q_window, mp_vol)
    out["atrp_pct"] = past_pct_rank(atrp, fp.q_window, mp_vol)
    out["vol_pct"] = 0.5 * out["bbw_pct"] + 0.5 * out["atrp_pct"]

    # fuel: relative dOI over k candles, both endpoints fresh
    k = fp.oi_lookback
    out["dprice"] = c / c.shift(k) - 1
    if oi_col in df.columns:
        oi = df[oi_col]
        if oi_col == "oi" and "oi_fresh" in df.columns:
            fresh = df["oi_fresh"]
        elif "bar_missing" in df.columns:
            fresh = oi.notna() & ~df["bar_missing"]  # same staleness rule as the main run
        else:
            fresh = oi.notna()
        doi = (oi - oi.shift(k)) / oi.shift(k)
        doi_ok = fresh & fresh.shift(k, fill_value=False)
        out["doi"] = doi.where(doi_ok)
    else:
        out["doi"] = np.nan  # degrade path: no OI at all

    dz_doi = out["doi"].abs().rolling(fp.q_window, min_periods=fp.q_min_periods_fuel) \
        .quantile(fp.dead_q).shift(1)
    dz_dp = out["dprice"].abs().rolling(fp.q_window, min_periods=mp_vol) \
        .quantile(fp.dead_q).shift(1)

    # quadrant codes: 0 no-fuel/deadzone/missing, 1 new-longs, 2 new-shorts,
    #                 3 short-covering, 4 long-liquidation
    d, p = out["doi"], out["dprice"]
    valid = d.notna() & dz_doi.notna() & dz_dp.notna() & (d.abs() >= dz_doi) & (p.abs() >= dz_dp)
    quad = np.select(
        [valid & (d > 0) & (p > 0), valid & (d > 0) & (p < 0),
         valid & (d < 0) & (p > 0), valid & (d < 0) & (p < 0)],
        [1, 2, 3, 4], default=0)
    out["quadrant"] = quad
    out["fuel_defined"] = d.notna() & dz_doi.notna()  # OI usable at all (deadzone still counts)

    # funding percentile (strictly-past); label-inert by design, confidence-only
    if "funding" in df.columns and df["funding"].notna().any():
        out["funding_pct"] = past_pct_rank(df["funding"], fp.q_window, fp.q_min_periods_fuel)
    else:
        out["funding_pct"] = np.nan

    out["close"] = c
    out["bar_missing"] = df["bar_missing"] if "bar_missing" in df.columns else False
    return out
