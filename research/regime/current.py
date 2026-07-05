#!/usr/bin/env python3
"""Live snapshot CLI: what regime are we in RIGHT NOW?

Pulls recent data from Binance USDT-M public REST (no key):
  - 60d of 15m klines   (vol percentile window 30d + indicator warmup)
  - 30d of 5m OI        (REST openInterestHist history cap — enough: fuel needs k=8 candles
                         plus a 20d+ deadzone quantile window)
  - recent funding
then classifies the LAST COMPLETED candle only (in-progress candle is discarded — labels are
stamped at close time, always) and prints regime + confidence + playbook.

History research uses the Vision archive (build_dataset.py); this path is live-only.
Usage: python current.py [BTCUSDT]
"""
import datetime as dt
import sys

import numpy as np
import pandas as pd
import requests

import features
import classifier
from playbook import REGIME_PLAYBOOK

FAPI = "https://fapi.binance.com"
BAR_MS = 15 * 60 * 1000


def _get(path: str, **params):
    r = requests.get(FAPI + path, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_klines(symbol: str, days: int = 60) -> pd.DataFrame:
    rows = []
    end = None
    need = days * 96
    while len(rows) < need:
        params = dict(symbol=symbol, interval="15m", limit=1500)
        if end:
            params["endTime"] = end
        chunk = _get("/fapi/v1/klines", **params)
        if not chunk:
            break
        rows = chunk + rows
        end = chunk[0][0] - 1
    k = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume",
                                    "ct", "qv", "n", "tb", "tq", "ig"])
    k = k.drop_duplicates("open_time").sort_values("open_time")
    for c in ["open", "high", "low", "close", "volume"]:
        k[c] = k[c].astype("float64")
    k["ts"] = pd.to_datetime(k["open_time"], unit="ms", utc=True) + pd.Timedelta(minutes=15)
    # drop the in-progress candle: its close time is in the future
    now = pd.Timestamp.now(tz="UTC")
    k = k[k["ts"] <= now]
    return k.set_index("ts")[["open", "high", "low", "close", "volume"]]


def fetch_oi(symbol: str) -> pd.DataFrame:
    """Page BACKWARD with endTime only — the endpoint 400s if startTime grazes its 30d cap."""
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    floor = now - int(29.5 * 86400 * 1000)
    rows, end = [], now
    while end > floor:
        chunk = _get("/futures/data/openInterestHist", symbol=symbol, period="5m",
                     limit=500, endTime=end)
        if not chunk:
            break
        rows = chunk + rows
        first = int(chunk[0]["timestamp"])
        if first >= end:
            break
        end = first - 1
    m = pd.DataFrame(rows)
    if m.empty:
        return pd.DataFrame(columns=["snap_ts", "oi"])
    m["snap_ts"] = pd.to_datetime(m["timestamp"].astype("int64"), unit="ms", utc=True)
    m["oi"] = m["sumOpenInterest"].astype("float64")
    return m.drop_duplicates("snap_ts").sort_values("snap_ts")[["snap_ts", "oi"]]


def fetch_funding(symbol: str) -> pd.DataFrame:
    rows = _get("/fapi/v1/fundingRate", symbol=symbol, limit=1000)
    fu = pd.DataFrame(rows)
    fu["event_ts"] = pd.to_datetime(fu["fundingTime"].astype("int64"), unit="ms", utc=True)
    fu["funding"] = fu["fundingRate"].astype("float64")
    return fu.sort_values("event_ts")[["event_ts", "funding"]]


def build_live_frame(symbol: str) -> pd.DataFrame:
    k = fetch_klines(symbol)
    m = fetch_oi(symbol)
    fu = fetch_funding(symbol)
    df = k.reset_index()
    df["ts"] = df["ts"].astype("datetime64[ns, UTC]")
    m["snap_ts"] = m["snap_ts"].astype("datetime64[ns, UTC]")
    fu["event_ts"] = fu["event_ts"].astype("datetime64[ns, UTC]")
    df = pd.merge_asof(df, m.rename(columns={"snap_ts": "ts"}), on="ts",
                       direction="backward", tolerance=pd.Timedelta(minutes=25))
    df = pd.merge_asof(df, fu.rename(columns={"event_ts": "ts"}), on="ts",
                       direction="backward")
    df = df.set_index("ts")
    df["bar_missing"] = df["close"].isna()
    df["oi_fresh"] = df["oi"].notna()
    return df


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows cp949 console
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    df = build_live_frame(symbol)
    feat = features.compute(df)
    res = classifier.classify(feat)
    ok = res["regime"].notna()
    if not ok.any():
        sys.exit("분류 가능한 캔들이 없습니다 (데이터 부족)")
    last = res.loc[ok].iloc[-1]
    ts = res.loc[ok].index[-1]

    regime, conf = last["regime"], last["confidence"]
    pb = REGIME_PLAYBOOK[regime]
    day = res.loc[ok].iloc[-96:]
    conf_now_vs_day = conf - day["confidence"].mean()
    changes = day["regime"][day["regime"] != day["regime"].shift()]

    print(f"=== {symbol} 레짐 스냅샷 (마지막 완결 15m 캔들) ===")
    print(f"시각(UTC)   : {ts:%Y-%m-%d %H:%M}")
    print(f"현재 레짐   : {regime}")
    print(f"confidence  : {conf:.3f}  (24h 평균 대비 {conf_now_vs_day:+.3f})")
    print(f"연료축(OI)  : {'사용 중' if last['fuel_available'] else '결측 → degrade 모드'}")
    print(f"24h 레짐 변화: {' → '.join(changes.tolist()) if len(changes) > 1 else '변화 없음'}")
    print()
    print(f"플레이북 [{regime}]")
    print(f"  주력   : {pb['primary']}")
    print(f"  보조   : {', '.join(pb['secondary'])}")
    print(f"  금지   : {', '.join(pb['avoid'])}")
    if conf < np.nanquantile(res.loc[ok, 'confidence'].to_numpy()[-2880:], 0.2):
        print("\n⚠ confidence가 최근 30일 하위 20% — 축 불일치, 전환 가능성. 사이즈 축소 권고.")


if __name__ == "__main__":
    main()
