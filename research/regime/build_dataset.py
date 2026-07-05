#!/usr/bin/env python3
"""Assemble the per-symbol 15m research dataset from raw Binance Vision zips.

ALL timestamp/alignment conventions live HERE and nowhere else:
- canonical candle close = open_time + 15m; the archive's close_time field (open+15m-1ms) is never read
- every timestamp is normalized to tz-aware UTC at load; metrics CSVs carry naive datetime strings
  and are parsed with utc=True explicitly (a KST-local parse would shift OI 9h into the future)
- kline epoch columns are milliseconds until 2025-01 and MICROSECONDS after; unit is detected
  per file by magnitude
- OI joined with merge_asof(direction="backward", tolerance=25min); the snapshot exactly at close
  IS included; beyond tolerance the candle has no OI (degrade path), never a stale fill
- klines are reindexed to a complete 15m UTC grid; missing bars flagged, never interpolated
- funding is snapped to the 8h UTC grid (assert offset < 60s) and applied to candles closing
  at-or-after the event (backward merge)

Usage: python build_dataset.py BTCUSDT [ETHUSDT ...]
Writes  data/<SYMBOL>_15m.parquet  and  data/<SYMBOL>_gaps.json
"""
import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
OUT = HERE / "data"
BAR = pd.Timedelta(minutes=15)
OI_TOLERANCE = pd.Timedelta(minutes=25)

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume",
              "close_time_unused", "quote_volume", "count",
              "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def _tag(p: Path) -> str:
    """Trailing date portion of a Vision zip stem: YYYY-MM (monthly) or YYYY-MM-DD (daily)."""
    parts = p.stem.split("-")
    if len(parts) >= 3 and parts[-3].isdigit() and len(parts[-3]) == 4:
        return "-".join(parts[-3:])
    return "-".join(parts[-2:])


def _in_range(p: Path, start: str | None, end: str | None) -> bool:
    t = _tag(p)
    return (start is None or t >= start[: len(t)]) and (end is None or t <= end[: len(t)])


def _read_zip_csv(path: Path, **kw) -> pd.DataFrame:
    """Read the single CSV inside a Vision zip, tolerating an optional header row."""
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        raw = z.read(name)
    first = raw.split(b"\n", 1)[0]
    has_header = not first.split(b",")[0].strip().replace(b".", b"").replace(b"-", b"").isdigit()
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None, **kw)


def _as_ns(s: pd.Series) -> pd.Series:
    """Normalize any tz-aware datetime series to ns resolution (pandas 3 keeps ms/us units)."""
    return s.astype("datetime64[ns, UTC]")


def _epoch_to_utc(series: pd.Series) -> pd.Series:
    """Epoch ints of unknown unit (ms until 2025-01, us after) -> tz-aware UTC (ns)."""
    v = series.astype("int64")
    med = float(v.median())
    if med > 1e14:  # microseconds
        v = v // 1000
    return _as_ns(pd.to_datetime(v, unit="ms", utc=True))


def read_klines_raw(symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Kline rows keyed by open_ts (pre-grid) — the cut/poison unit for look-ahead tests."""
    files = [f for f in sorted((RAW / symbol / "klines").glob("*.zip")) if _in_range(f, start, end)]
    if not files:
        raise FileNotFoundError(f"no kline zips for {symbol}")
    parts = []
    for f in files:
        df = _read_zip_csv(f)
        df.columns = KLINE_COLS[: len(df.columns)]
        df["open_ts"] = _epoch_to_utc(df["open_time"])
        parts.append(df[["open_ts", "open", "high", "low", "close", "volume"]])
    k = pd.concat(parts, ignore_index=True)
    for c in ["open", "high", "low", "close", "volume"]:
        k[c] = k[c].astype("float64")
    k = k.sort_values("open_ts")
    dup = k.duplicated("open_ts", keep=False)
    if dup.any():
        # monthly/daily overlap: assert identical rows, then drop
        g = k[dup].groupby("open_ts").agg(["min", "max"])
        mismatch = 0
        for c in ["open", "high", "low", "close", "volume"]:
            mismatch += int((g[(c, "min")] != g[(c, "max")]).sum())
        assert mismatch == 0, f"{symbol}: {mismatch} conflicting duplicate kline rows"
        k = k.drop_duplicates("open_ts", keep="first")
    return k.reset_index(drop=True)


def grid_klines(kraw: pd.DataFrame) -> pd.DataFrame:
    """Canonical close time (open+15m, never the archive close_time field), complete grid."""
    k = kraw.copy()
    k["ts"] = k["open_ts"] + BAR
    k = k.set_index("ts").drop(columns=["open_ts"]).sort_index()
    grid = pd.date_range(k.index[0], k.index[-1], freq="15min", tz="UTC", unit="ns")
    k = k.reindex(grid)
    k.index.name = "ts"
    k["bar_missing"] = k["close"].isna()
    return k


def read_metrics(symbol: str, start: str | None = None, end: str | None = None):
    files = [f for f in sorted((RAW / symbol / "metrics").glob("*.zip")) if _in_range(f, start, end)]
    if not files:
        raise FileNotFoundError(f"no metrics zips for {symbol}")
    METRIC_COLS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                   "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                   "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
    parts = []
    for f in files:
        df = _read_zip_csv(f)
        if df.columns.dtype != object or "create_time" not in df.columns:
            df.columns = METRIC_COLS[: len(df.columns)]
        parts.append(df[[c for c in METRIC_COLS if c in df.columns and c != "symbol"]])
    m = pd.concat(parts, ignore_index=True)
    # naive datetime strings -> explicit UTC (never local time)
    m["snap_ts"] = _as_ns(pd.to_datetime(m["create_time"], utc=True, format="mixed"))
    m["oi"] = m["sum_open_interest"].astype("float64")
    # positioning/flow companions riding the same snapshots (free, same causality rules)
    for src, dst in [("sum_toptrader_long_short_ratio", "lsr_top"),
                     ("count_long_short_ratio", "lsr_all"),
                     ("sum_taker_long_short_vol_ratio", "taker_ratio")]:
        m[dst] = m[src].astype("float64") if src in m.columns else np.nan
    m = m.sort_values("snap_ts")
    # snapshots are creation times with seconds-level jitter off the 5m grid (measured on the
    # real archive); keep RAW times (they are availability times — no snapping) but assert the
    # cadence is intact: median spacing 5min, few big holes
    d = m["snap_ts"].diff().dropna()
    med = d.median()
    assert pd.Timedelta(minutes=4, seconds=30) <= med <= pd.Timedelta(minutes=5, seconds=30), \
        f"{symbol}: OI snapshot median spacing {med} != ~5min"
    n_offgrid = int((d > pd.Timedelta(minutes=10)).sum())  # count of cadence holes (info only)
    dup = m.duplicated("snap_ts", keep=False)
    n_conflict = 0
    if dup.any():
        g = m[dup].groupby("snap_ts")["oi"].agg(["min", "max"])
        rel = (g["max"] - g["min"]) / g["max"].replace(0, np.nan)
        n_conflict = int((rel > 0.001).sum())
        m = m.drop_duplicates("snap_ts", keep="first")
    keep = ["snap_ts", "oi", "lsr_top", "lsr_all", "taker_ratio"]
    return m[keep].reset_index(drop=True), n_offgrid, n_conflict


def read_funding(symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    files = [f for f in sorted((RAW / symbol / "funding").glob("*.zip")) if _in_range(f, start, end)]
    parts = []
    for f in files:
        df = _read_zip_csv(f)
        if df.columns.dtype != object or "calc_time" not in df.columns:
            df.columns = ["calc_time", "funding_interval_hours", "last_funding_rate"][: len(df.columns)]
        parts.append(df[["calc_time", "last_funding_rate"]])
    if not parts:
        return pd.DataFrame(columns=["event_ts", "funding"])
    # unit detection must be per-file like klines: a mid-archive ms->us switch would poison
    # a single concat-level median
    for p_ in parts:
        p_["raw_ts"] = _epoch_to_utc(p_["calc_time"])
    fu = pd.concat(parts, ignore_index=True)
    # funding boundaries land on the hour, but the INTERVAL is not always 8h (Binance switches
    # some symbols to 4h in volatile periods — hence the funding_interval_hours column).
    # Snap to the 1h grid; the backward merge is causal regardless of cadence.
    snapped = fu["raw_ts"].dt.round("1h")
    offset = (fu["raw_ts"] - snapped).abs()
    assert (offset < pd.Timedelta(seconds=60)).all(), \
        f"{symbol}: funding event >60s off the 1h grid (max {offset.max()})"
    fu["event_ts"] = snapped
    fu["funding"] = fu["last_funding_rate"].astype("float64")
    fu = fu.sort_values("event_ts").drop_duplicates("event_ts", keep="first")
    return fu[["event_ts", "funding"]].reset_index(drop=True)


def runs_of(mask: pd.Series) -> list:
    """Contiguous True-runs of a boolean series -> [(start_iso, end_iso, n), ...]."""
    out = []
    idx = mask.index
    arr = mask.to_numpy()
    start = None
    for i, v in enumerate(arr):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((str(idx[start]), str(idx[i - 1]), i - start))
            start = None
    if start is not None:
        out.append((str(idx[start]), str(idx[-1]), len(arr) - start))
    return out


def assemble(kraw: pd.DataFrame, m: pd.DataFrame, fu: pd.DataFrame) -> pd.DataFrame:
    """Join the three raw tables into the final candle frame. Pure function of its inputs —
    the look-ahead tests cut/poison the raw tables and re-run exactly this."""
    k = grid_klines(kraw)
    df = k.reset_index()
    # OI: last snapshot at-or-before candle close, within tolerance (boundary snapshot included)
    df = pd.merge_asof(df, m.rename(columns={"snap_ts": "ts"}), on="ts",
                       direction="backward", tolerance=OI_TOLERANCE)
    # OI lagged by one 5m snapshot (publication-latency robustness run): last snapshot
    # at-or-before close-5min
    lag = pd.merge_asof(
        k.reset_index()[["ts"]].assign(ts_l=lambda d: d["ts"] - pd.Timedelta(minutes=5)),
        m[["snap_ts", "oi"]].rename(columns={"snap_ts": "ts_l", "oi": "oi_lag1"}),
        on="ts_l", direction="backward", tolerance=OI_TOLERANCE)
    df["oi_lag1"] = lag["oi_lag1"].to_numpy()
    # funding: last event at-or-before close (rate known at event time, applied thereafter)
    df = pd.merge_asof(df, fu.rename(columns={"event_ts": "ts"}), on="ts",
                       direction="backward")
    df = df.set_index("ts")
    df["oi_fresh"] = df["oi"].notna() & ~df["bar_missing"]
    return df


def build(symbol: str) -> None:
    kraw = read_klines_raw(symbol)
    m, n_offgrid, n_conflict = read_metrics(symbol)
    fu = read_funding(symbol)
    df = assemble(kraw, m, fu)

    gaps = {
        "bar_missing_runs": runs_of(df["bar_missing"]),
        "oi_missing_runs": runs_of(~df["oi_fresh"]),
    }
    (OUT / f"{symbol}_gaps.json").write_text(json.dumps(gaps, indent=1))
    df.to_parquet(OUT / f"{symbol}_15m.parquet")

    oi_cov = df["oi_fresh"].mean()
    print(f"{symbol}: {len(df)} bars  {df.index[0]} .. {df.index[-1]}")
    print(f"  bar_missing: {int(df['bar_missing'].sum())}  "
          f"oi coverage: {oi_cov:.3%}  oi gaps(runs): {len(gaps['oi_missing_runs'])}  "
          f"offgrid snaps dropped: {n_offgrid}  conflicting dup snaps: {n_conflict}")
    print(f"  funding events: {df['funding'].notna().sum() and len(fu)}  "
          f"first: {fu['event_ts'].iloc[0] if len(fu) else 'n/a'}")
    top = sorted(gaps["oi_missing_runs"], key=lambda r: -r[2])[:5]
    for s, e, n in top:
        print(f"  oi gap {n:5d} bars  {s} .. {e}")


if __name__ == "__main__":
    for sym in sys.argv[1:] or ["BTCUSDT"]:
        build(sym)
