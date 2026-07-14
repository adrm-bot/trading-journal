#!/usr/bin/env python3
"""regime.py — REGIME v2.1 시장 레짐·별도 OI 포지셔닝 + '레짐별 내 성과'.

운영 레짐은 research/regime의 canonical 15m 가격 2축(direction + volatility)만 소비한다.
OI·funding은 레짐 라벨·확신에 절대 관여하지 않고 별도 포지셔닝 카드에서만 표시한다.
5m·15m·1h·4h 화면은 같은 확정 15m Core 스냅샷을 사용하며, 1h·4h 정보는 확정봉의
설명용 가격 context일 뿐 방향 예측·진입·레버리지 신호가 아니다. v1 classify2는 기존
연구·회귀 테스트 호환을 위해 남기되 운영 live/perf 경로에서는 사용하지 않는다.

레짐별 성과는 **진입 시각(opened_at) 기준** 매칭(없으면 청산 시각 폴백) — 바이낸스 무기한에
없는 심볼의 거래는 '미매칭'으로 정직하게 따로 센다. 모든 실패는 available:false류로 강등되고
앱은 계속 뜬다(시장 카드들과 동일 규약).
"""
import logging
import os
import threading
import time

import numpy as np
import pandas as pd
import requests

from research.regime.classifier import classify_v2 as research_classify_v2
from research.regime.features import (V2_CORE_MINUTES_BY_CHART, compute_v2,
                                      v2_params_for_chart)

log = logging.getLogger("regime")

FAPI = "https://fapi.binance.com"
REGIMES = ("TREND_UP", "TREND_DOWN", "SQUEEZE", "RANGE", "CHOP")
NAME = {"TREND_UP": "상승추세", "TREND_DOWN": "하락추세", "SQUEEZE": "수렴(돌파대기)",
        "RANGE": "박스권", "CHOP": "혼조(관망)"}
EMOJI = {r: "●" for r in REGIMES}
SUPER = {"TREND_UP": "UP", "TREND_DOWN": "DOWN"}

# 리서치 동결 파라미터 (창은 벽시계 기준, TF별 봉수 환산)
ADX_N, EMA_F, EMA_S, BB_N, BB_K = 14, 20, 50, 20, 2.0
ADX_LO, ADX_SCALE = 20.0, 15.0
W_DIR, W_VOL, W_FUEL, NEAR_TIE = 0.5, 0.3, 0.2, 0.02
RAMP_LO, RAMP_HI = (0.15, 0.25), (0.75, 0.85)
DEAD_Q = 0.25
QUAD_TEXT = {1: "신규 롱 가능 · 가격·OI 동반 상승",
             2: "신규 숏 가능 · 가격 하락·OI 증가",
             3: "숏 커버 가능 · 가격 상승·OI 감소",
             4: "롱 청산 가능 · 가격·OI 동반 하락",
             0: "가격·OI 변화 기준 미달"}

_TTL = 300
_live_cache: dict = {}  # {symbol: {"at": ts, "data": payload}}
_labels_cache: dict = {}   # sym -> {"at": ts, "since": Timestamp, "s": Series|None}
_oi_cache: dict = {}       # sym -> {"at": ts, "data": DataFrame|None}
_positioning_cache: dict = {}  # sym -> {"at": ts, "data": payload}
_lock = threading.Lock()
_oi_lock = threading.Lock()

POSITIONING_SYMBOLS = tuple(dict.fromkeys(
    s.strip().upper() for s in os.getenv(
        "POSITIONING_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT"
    ).split(",") if s.strip()
))

V2_FP = v2_params_for_chart(15)


# ── 데이터 ───────────────────────────────────────────────────────────────────
def _grid(interval: str) -> int:
    return {"15m": 15, "1h": 60, "4h": 240, "12h": 720, "1d": 1440}[interval]


def _fetch_klines(symbol: str, interval: str, start_ms: int | None = None,
                  days: int | None = None, session=None) -> pd.DataFrame | None:
    """공개 klines. start_ms부터 전진 페이지네이션(라벨용) 또는 최근 days만(라이브용).
    심볼 없음(400/404) → None."""
    s = session or requests
    tf = _grid(interval)
    rows = []
    try:
        if start_ms is None:
            need = (days or 60) * (1440 // tf)
            end = None
            while len(rows) < need:
                params = {"symbol": symbol, "interval": interval, "limit": 1500}
                if end:
                    params["endTime"] = end
                r = s.get(f"{FAPI}/fapi/v1/klines", params=params, timeout=30)
                if r.status_code in (400, 404):
                    return None
                r.raise_for_status()
                chunk = r.json()
                if not chunk:
                    break
                rows = chunk + rows
                end = chunk[0][0] - 1
        else:
            cur = start_ms
            for _ in range(400):  # 400×1500봉 상한 — 폭주 방지
                r = s.get(f"{FAPI}/fapi/v1/klines",
                          params={"symbol": symbol, "interval": interval,
                                  "startTime": cur, "limit": 1500}, timeout=30)
                if r.status_code in (400, 404):
                    return None
                r.raise_for_status()
                chunk = r.json()
                if not chunk:
                    break
                rows += chunk
                nxt = chunk[-1][0] + tf * 60_000
                if nxt <= cur or len(chunk) < 1500:
                    break
                cur = nxt
    except Exception as e:  # noqa: BLE001 — 네트워크 실패는 카드 강등으로
        log.warning("klines 실패 %s %s: %s", symbol, interval, e)
        return None
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ot", "open", "high", "low", "close", "vol",
                                     "ct", "qv", "n", "tb", "tq", "ig"])
    df = df.drop_duplicates("ot").sort_values("ot")
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype("float64")
    idx = pd.to_datetime(df["ot"].astype("int64"), unit="ms", utc=True) + pd.Timedelta(minutes=tf)
    out = df[["open", "high", "low", "close"]].set_axis(idx, axis=0)
    # 마지막 미완결 캔들 제거(라벨은 종가 확정 기준)
    now = pd.Timestamp.now(tz="UTC")
    return out[out.index <= now]


def _fetch_oi(symbol: str, session=None) -> pd.DataFrame | None:
    """최근 ~29.5일 5분 OI(공개 REST, endTime 후진 페이지네이션 — 30일 캡 경계 400 회피).
    라이브 판정 전용 — 과거 성과 라벨에는 30일 한도라 사용 불가(정직 표기)."""
    s = session or requests
    now = int(time.time() * 1000)
    floor = now - int(29.5 * 86400 * 1000)
    rows, end = [], now
    try:
        while end > floor:
            r = s.get(f"{FAPI}/futures/data/openInterestHist",
                      params={"symbol": symbol, "period": "5m", "limit": 500, "endTime": end},
                      timeout=30)
            if r.status_code in (400, 404):
                return None
            r.raise_for_status()
            chunk = r.json()
            if not chunk:
                break
            rows = chunk + rows
            first = int(chunk[0]["timestamp"])
            if first >= end:
                break
            end = first - 1
    except Exception as e:  # noqa: BLE001
        log.warning("OI 실패 %s: %s", symbol, e)
        return None
    if not rows:
        return None
    m = pd.DataFrame(rows)
    m["snap_ts"] = pd.to_datetime(m["timestamp"].astype("int64"), unit="ms", utc=True)
    m["oi"] = m["sumOpenInterest"].astype("float64")
    # USD 명목가치를 백분위 분류와 화면 표시에 우선 사용하고, 없으면 계약수를 대체값으로 쓴다.
    m["oi_usd"] = pd.to_numeric(m.get("sumOpenInterestValue"), errors="coerce")
    return m.drop_duplicates("snap_ts").sort_values("snap_ts")[["snap_ts", "oi", "oi_usd"]]


def _cached_oi(symbol: str) -> pd.DataFrame | None:
    """레짐과 포지셔닝 카드가 같은 5분 OI 스냅샷을 공유한다.

    FastAPI 동시 요청에서 레짐/OI 카드가 각각 30일 데이터를 중복 조회하지 않도록
    네트워크 구간까지 짧은 전용 락으로 직렬화한다.
    """
    now = time.time()
    hit = _oi_cache.get(symbol)
    if hit is not None and now - hit["at"] < _TTL:
        return hit["data"]
    with _oi_lock:
        hit = _oi_cache.get(symbol)
        if hit is not None and now - hit["at"] < _TTL:
            return hit["data"]
        data = _fetch_oi(symbol)
        _oi_cache[symbol] = {"at": time.time(), "data": data}
        return data


def _fetch_public_series(path: str, symbol: str, period: str = "5m", limit: int = 500,
                         session=None) -> list[dict]:
    """Binance USDⓈ-M 공개 통계 시계열. 실패는 빈 리스트로 강등한다."""
    s = session or requests
    try:
        r = s.get(f"{FAPI}{path}", params={"symbol": symbol, "period": period, "limit": limit},
                  timeout=30)
        if r.status_code in (400, 404):
            return []
        r.raise_for_status()
        rows = r.json()
        return sorted((x for x in rows if isinstance(x, dict)),
                      key=lambda x: int(x.get("timestamp") or 0))
    except Exception as e:  # noqa: BLE001
        log.warning("공개 포지셔닝 실패 %s %s: %s", path, symbol, e)
        return []


def _series_change(s: pd.Series, delta: pd.Timedelta) -> float | None:
    s = s.dropna().sort_index()
    if len(s) < 2:
        return None
    cur = float(s.iloc[-1])
    old = s.asof(s.index[-1] - delta)
    if pd.isna(old) or not float(old):
        return None
    return round((cur / float(old) - 1.0) * 100, 2)


def _row_at(rows: list[dict], delta: pd.Timedelta) -> dict | None:
    if not rows:
        return None
    end = int(rows[-1].get("timestamp") or 0)
    target = end - int(delta.total_seconds() * 1000)
    prior = [r for r in rows if int(r.get("timestamp") or 0) <= target]
    return prior[-1] if prior else None


def _fnum(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _brief_oi(symbol: str) -> dict | None:
    """비교 심볼은 1시간×7일 한 번만 조회해 카드 부하를 제한한다."""
    rows = _fetch_public_series("/futures/data/openInterestHist", symbol, "1h", 169)
    if not rows:
        return None
    vals = [(_fnum(r.get("sumOpenInterestValue")), int(r.get("timestamp") or 0)) for r in rows]
    vals = [(v, ts) for v, ts in vals if v is not None]
    if not vals:
        return None
    cur = vals[-1][0]
    old = vals[-25][0] if len(vals) >= 25 else vals[0][0]
    return {"sym": symbol.replace("USDT", ""), "usd": round(cur, 2),
            "chg24": round((cur / old - 1) * 100, 2) if old else None}


def classify_oi_risk(pct_rank: float | None, tf: dict | None,
                     long_pct: float | None = None, short_pct: float | None = None,
                     taker_buy_pct: float | None = None,
                     taker_sell_pct: float | None = None,
                     sample: dict | None = None) -> dict:
    """현재 OI 명목가치의 경험적 백분위를 설명용 세 구간으로 나눈다.

    증가 속도·계정 쏠림·테이커 체결은 설명용 보조 맥락으로만 반환하며 단계
    판정에는 관여하지 않는다. 70·90백분위는 예측 모델로 검증한 임계값이 아니라
    최근 분포의 상위 30%·10%를 구분하는 기술적(descriptive) 분위 기준이다.
    """
    tf = tf or {}
    sample = sample or {}
    sample_meta = {
        "sample_n": sample.get("n"),
        "sample_days": sample.get("days"),
        "sample_interval_minutes": sample.get("interval_minutes"),
        "sample_start": sample.get("start"),
        "sample_end": sample.get("end"),
        "measurement": sample.get("measurement", "usd_notional"),
        "threshold_kind": "descriptive_quantile",
        "thresholds_validated": False,
    }
    if pct_rank is None:
        return {
            "level": "unknown", "label": "평가 보류", "active_count": 0,
            "factor_count": 1, "aux_active_count": 0, "aux_factor_count": 3,
            "vulnerable_side": None, "factors": [],
            "quantity_percentile": None, "quantity_only": True,
            "thresholds": {"watch": 70, "risk": 90},
            "basis": "현재 OI 명목가치의 최근 가용 표본 경험적 백분위",
            "disclaimer": "OI 분포 데이터가 부족해 규모 단계를 평가하지 않습니다.",
            **sample_meta,
        }

    oi_level_on = pct_rank >= 90
    build_candidates = (("1H", tf.get("1h"), 1.0),
                        ("4H", tf.get("4h"), 2.0),
                        ("24H", tf.get("24h"), 5.0))
    build_hits = [(label, value) for label, value, threshold in build_candidates
                  if value is not None and value >= threshold]
    oi_build_on = bool(build_hits)
    build_label, build_value = (build_hits[0] if build_hits else
                                next(((label, value) for label, value, _ in reversed(build_candidates)
                                      if value is not None), ("24H", None)))

    crowd_side = ("long" if long_pct is not None and long_pct >= 60 else
                  "short" if short_pct is not None and short_pct >= 60 else None)
    crowd_value = long_pct if crowd_side == "long" else short_pct if crowd_side == "short" else (
        max(v for v in (long_pct, short_pct) if v is not None)
        if any(v is not None for v in (long_pct, short_pct)) else None)
    crowd_on = crowd_side is not None

    taker_side = ("buy" if taker_buy_pct is not None and taker_buy_pct >= 60 else
                  "sell" if taker_sell_pct is not None and taker_sell_pct >= 60 else None)
    taker_value = taker_buy_pct if taker_side == "buy" else taker_sell_pct if taker_side == "sell" else (
        max(v for v in (taker_buy_pct, taker_sell_pct) if v is not None)
        if any(v is not None for v in (taker_buy_pct, taker_sell_pct)) else None)
    taker_on = taker_side is not None

    factors = [
        {"key": "oi_level", "label": "OI 규모",
         "value": f"30일 {round(pct_rank)}백분위", "active": oi_level_on},
        {"key": "oi_build", "label": "OI 증가 속도",
         "value": (f"{build_label} {build_value:+.2f}%" if build_value is not None else "데이터 없음"),
         "active": oi_build_on},
        {"key": "account_crowd", "label": "계정 비중 쏠림",
         "value": (f"{'롱' if crowd_side == 'long' else '숏' if crowd_side == 'short' else '최대'} "
                   f"{crowd_value:.1f}%" if crowd_value is not None else "데이터 없음"),
         "active": crowd_on},
        {"key": "taker_flow", "label": "테이커 체결 쏠림",
         "value": (f"{'매수' if taker_side == 'buy' else '매도' if taker_side == 'sell' else '최대'} "
                   f"{taker_value:.1f}%" if taker_value is not None else "데이터 없음"),
         "active": taker_on},
    ]
    aux_active_count = sum(bool(f["active"]) for f in factors if f["key"] != "oi_level")
    # 단계는 오직 현재 OI 양의 30일 백분위로 정한다. 아래 보조 신호는 승급시키지 않는다.
    if pct_rank >= 90:
        level, label = "risk", "매우 높은 구간"
    elif pct_rank >= 70:
        level, label = "watch", "높은 구간"
    else:
        level, label = "safe", "보통 구간"
    return {
        "level": level, "label": label, "active_count": int(oi_level_on),
        "factor_count": 1, "aux_active_count": aux_active_count,
        "aux_factor_count": len(factors) - 1, "vulnerable_side": crowd_side,
        "factors": factors, "quantity_percentile": round(float(pct_rank), 2),
        "quantity_only": True, "thresholds": {"watch": 70, "risk": 90},
        "exceedance_pct": round(max(0.0, 100.0 - float(pct_rank)), 2),
        "basis": "현재 BTCUSDT OI 명목가치의 최근 가용 표본 경험적 백분위",
        "disclaimer": ("70·90백분위는 최근 분포의 상위 30%·10%를 나눈 설명용 기준입니다. "
                       "스퀴즈·청산 확률이나 수익률로 검증한 임계값이 아니며, 증가 속도·계정 비중·"
                       "테이커 체결은 단계 판정에 반영하지 않습니다."),
        **sample_meta,
    }


def build_positioning(oi: pd.DataFrame | None, ratio_rows: list[dict] | None = None,
                      taker_rows: list[dict] | None = None, by_symbol: list[dict] | None = None,
                      symbol: str = "BTCUSDT", ratio_week_rows: list[dict] | None = None) -> dict:
    """OI·계정 비중·테이커 체결을 한 스냅샷으로 정규화하는 순수 조립 함수."""
    ratio_rows, taker_rows, ratio_week_rows = ratio_rows or [], taker_rows or [], ratio_week_rows or []
    out = {"available": False, "symbol": symbol, "source": "Binance USDⓈ-M",
           "window_days": 30, "tf": {}, "by_symbol": by_symbol or []}

    if oi is not None and not oi.empty:
        frame = oi.drop_duplicates("snap_ts").sort_values("snap_ts").copy()
        col = "oi_usd" if "oi_usd" in frame and frame["oi_usd"].notna().any() else "oi"
        s = frame.set_index("snap_ts")[col].astype(float)
        if s.notna().any():
            cur = float(s.dropna().iloc[-1])
            hist = s.dropna()
            sample_days = max(0.0, (hist.index[-1] - hist.index[0]).total_seconds() / 86400)
            sample_interval = None
            if len(hist) > 1:
                diffs = hist.index.to_series().diff().dropna().dt.total_seconds() / 60
                if not diffs.empty:
                    sample_interval = round(float(diffs.median()), 2)
            out.update(available=True, oi_available=True,
                       pct_rank=round(float((hist <= cur).mean()) * 100),
                       asof=hist.index[-1].isoformat(), oi_unit=("usd" if col == "oi_usd" else "contracts"),
                       sample_n=int(len(hist)), sample_days=round(sample_days, 2),
                       sample_interval_minutes=sample_interval,
                       sample_start=hist.index[0].isoformat(), sample_end=hist.index[-1].isoformat())
            out["total_usd" if col == "oi_usd" else "total_contracts"] = round(cur, 2)
            out["tf"] = {
                "1h": _series_change(hist, pd.Timedelta(hours=1)),
                "4h": _series_change(hist, pd.Timedelta(hours=4)),
                "24h": _series_change(hist, pd.Timedelta(days=1)),
                "7d": _series_change(hist, pd.Timedelta(days=7)),
            }
    else:
        out["oi_available"] = False

    current_ratio_rows = ratio_rows or ratio_week_rows
    if current_ratio_rows:
        last = current_ratio_rows[-1]
        lp, sp = _fnum(last.get("longAccount")), _fnum(last.get("shortAccount"))
        ratio = _fnum(last.get("longShortRatio"))
        if lp is not None and sp is not None:
            out.update(available=True, long_pct=round(lp * 100, 1), short_pct=round(sp * 100, 1),
                       ls_ratio=round(ratio if ratio is not None else (lp / sp if sp else 0), 3))
            deltas = {}
            for key, delta in (("1h", pd.Timedelta(hours=1)), ("4h", pd.Timedelta(hours=4)),
                               ("24h", pd.Timedelta(days=1))):
                old = _row_at(ratio_rows or current_ratio_rows, delta)
                old_lp = _fnum((old or {}).get("longAccount"))
                deltas[key] = None if old_lp is None else round((lp - old_lp) * 100, 2)
            old7 = _row_at(ratio_week_rows, pd.Timedelta(days=7))
            old7_lp = _fnum((old7 or {}).get("longAccount"))
            deltas["7d"] = None if old7_lp is None else round((lp - old7_lp) * 100, 2)
            out["long_delta_pp"] = deltas
            try:
                out["ratio_asof"] = pd.to_datetime(
                    int(last.get("timestamp") or 0), unit="ms", utc=True
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
            d1 = deltas.get("1h")
            out["crowd_trend"] = ("long_increasing" if d1 is not None and d1 >= 0.2 else
                                  "short_increasing" if d1 is not None and d1 <= -0.2 else "stable")
            out["crowd_risk"] = "long" if lp >= 0.60 else ("short" if sp >= 0.60 else None)

    if taker_rows:
        end = int(taker_rows[-1].get("timestamp") or 0)
        recent = [r for r in taker_rows if int(r.get("timestamp") or 0) >= end - 3600_000]
        buy = sum(_fnum(r.get("buyVol")) or 0 for r in recent)
        sell = sum(_fnum(r.get("sellVol")) or 0 for r in recent)
        total = buy + sell
        if total:
            out.update(available=True, taker_buy_pct_1h=round(buy / total * 100, 1),
                       taker_sell_pct_1h=round(sell / total * 100, 1),
                       taker_ratio_1h=round(buy / sell, 3) if sell else None)
    out["oi_risk"] = classify_oi_risk(
        out.get("pct_rank"), out.get("tf"), out.get("long_pct"), out.get("short_pct"),
        out.get("taker_buy_pct_1h"), out.get("taker_sell_pct_1h"), {
            "n": out.get("sample_n"), "days": out.get("sample_days"),
            "interval_minutes": out.get("sample_interval_minutes"),
            "start": out.get("sample_start"), "end": out.get("sample_end"),
            "measurement": "usd_notional" if out.get("oi_unit") == "usd" else "contracts",
        },
    )
    return out


def positioning(symbol: str = "BTCUSDT") -> dict:
    """대시보드 OI/롱숏 포지셔닝. 공개 API만 사용하며 5분 캐시한다."""
    now = time.time()
    hit = _positioning_cache.get(symbol)
    if hit is not None and now - hit["at"] < _TTL:
        return hit["data"]
    oi = _cached_oi(symbol)
    ratios = _fetch_public_series("/futures/data/globalLongShortAccountRatio", symbol)
    ratios_week = _fetch_public_series(
        "/futures/data/globalLongShortAccountRatio", symbol, "1h", 169
    )
    taker = _fetch_public_series("/futures/data/takerlongshortRatio", symbol)
    rows = []
    for sym in POSITIONING_SYMBOLS:
        if sym == symbol and oi is not None and not oi.empty and "oi_usd" in oi:
            s = oi.set_index("snap_ts")["oi_usd"].dropna()
            if len(s):
                rows.append({"sym": sym.replace("USDT", ""), "usd": round(float(s.iloc[-1]), 2),
                             "chg24": _series_change(s, pd.Timedelta(days=1))})
        else:
            row = _brief_oi(sym)
            if row:
                rows.append(row)
    data = build_positioning(oi, ratios, taker, rows, symbol, ratios_week)
    if not data["available"]:
        data["error"] = "Binance 공개 포지셔닝 데이터를 받지 못했습니다"
    _positioning_cache[symbol] = {"at": time.time(), "data": data}
    return data


# ── 2축 분류 (리서치 B2와 동일 수식) ─────────────────────────────────────────
def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _past_pct(s: pd.Series, w: int, mp: int) -> pd.Series:
    r = s.rolling(w, min_periods=mp).rank(pct=True)
    c = s.rolling(w, min_periods=mp).count()
    return ((r * c - 1) / (c - 1)).where(c > 1)


def _fuel_series(bars: pd.DataFrame, oi: pd.DataFrame, tf_min: int, qw: int, mp: int) -> dict:
    """ΔOI·Δprice·데드존·사분면 시계열 — classify2와 라이브 사분면 플롯이 같은 수식 공용."""
    c = bars["close"]
    k = max(1, 120 // tf_min)  # ΔOI lookback = 2시간
    left = pd.DataFrame({"ts": bars.index.astype("datetime64[ns, UTC]")})
    right = oi.rename(columns={"snap_ts": "ts"}).copy()
    right["ts"] = right["ts"].astype("datetime64[ns, UTC]")
    joined = pd.merge_asof(left, right.sort_values("ts"), on="ts", direction="backward",
                           tolerance=pd.Timedelta(minutes=25)).set_index("ts")["oi"]
    joined.index = bars.index
    fresh = joined.notna()
    doi = (joined / joined.shift(k) - 1.0).where(fresh & fresh.shift(k, fill_value=False))
    dpr = c / c.shift(k) - 1.0
    mp_fuel = max(2, min(20 * (1440 // tf_min), int(qw * 0.95)))
    dz_doi = doi.abs().rolling(qw, min_periods=mp_fuel).quantile(DEAD_Q).shift(1)
    dz_dpr = dpr.abs().rolling(qw, min_periods=mp).quantile(DEAD_Q).shift(1)
    d, p = doi.to_numpy(float), dpr.to_numpy(float)
    zd, zp = dz_doi.to_numpy(float), dz_dpr.to_numpy(float)
    on = ~np.isnan(d) & ~np.isnan(zd) & ~np.isnan(zp) & (np.abs(d) >= zd) & (np.abs(p) >= zp)
    quad = np.select([on & (d > 0) & (p > 0), on & (d > 0) & (p < 0),
                      on & (d < 0) & (p > 0), on & (d < 0) & (p < 0)], [1, 2, 3, 4], 0)
    return {"doi": doi, "dpr": dpr, "dz_doi": dz_doi, "dz_dpr": dz_dpr,
            "quad": quad, "fuel_defined": ~np.isnan(d) & ~np.isnan(zd)}


def _oi_context_quad(bars: pd.DataFrame, oi: pd.DataFrame | None) -> dict | None:
    """최근 가격·OI 사분면을 설명용 시장 맥락으로만 반환한다.

    이 값은 REGIME v2.1의 라벨·확신·상태 전환 계산에 입력되지 않는다. 기존
    사분면 시각화를 복원하되 price-only Core 계약과 명확히 분리하기 위한 경계다.
    """
    if oi is None or oi.empty:
        return None
    try:
        qwin = int(V2_FP.q_window)
        fs = _fuel_series(bars, oi, 15, qwin, int(qwin * 0.95))
        valid = fs["fuel_defined"]
        if not np.asarray(valid).any():
            return None
        code = int(fs["quad"][-1])
        points = []
        for x, y, qc in zip(fs["dpr"].iloc[-24:], fs["doi"].iloc[-24:], fs["quad"][-24:]):
            if pd.notna(x) and pd.notna(y):
                points.append({"x": round(float(x) * 100, 3),
                               "y": round(float(y) * 100, 3), "q": int(qc)})
        if not points:
            return None
        dzx, dzy = fs["dz_dpr"].iloc[-1], fs["dz_doi"].iloc[-1]
        return {"code": code, "text": QUAD_TEXT.get(code, QUAD_TEXT[0]),
                "plot": {"points": points,
                         "dz_x": round(float(dzx) * 100, 3) if pd.notna(dzx) else None,
                         "dz_y": round(float(dzy) * 100, 3) if pd.notna(dzy) else None},
                "lookback_hours": 6, "change_window_hours": 2,
                "separate_from_regime": True}
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _drift_state(close: pd.Series, bars_per_day: int) -> tuple[bool, float]:
    """표류(드리프트) 태그 — 표시층 전용, 분류기 출력 불변 (Pine v1.6·current.py 동일 수식).

    게이트(4년 BTC 15m 캘리브, research/regime/results/display_calib.json):
    on |24h 순이동|≥2% AND ER(24h)≥0.10 / off <1.5% or <0.08.
    정직성: '지난 24h' 과거 기술 — 표류-on 코호트 실측 전방지속 0.46~0.47(엣지 없음).
    의미: 박스 경계가 이동 중 → 레인지 페이드의 전제(레벨 정상성)가 깨졌다는 경고."""
    net = close / close.shift(bars_per_day) - 1.0
    er = (close - close.shift(bars_per_day)).abs() \
        / close.diff().abs().rolling(bars_per_day).sum().replace(0, np.nan)
    on = False
    nv, ev = net.to_numpy(float), er.to_numpy(float)
    for i in range(len(nv)):
        if np.isnan(nv[i]) or np.isnan(ev[i]):
            on = False
        elif not on and abs(nv[i]) >= 0.02 and ev[i] >= 0.10:
            on = True
        elif on and (abs(nv[i]) < 0.015 or ev[i] < 0.08):
            on = False
    # 갭 가드: '지난 24h' 표기가 참이려면 마지막 창이 정확히 24시간이어야 함 (REST 갭 방어)
    if len(close) > bars_per_day and \
            close.index[-1] - close.index[-1 - bars_per_day] != pd.Timedelta(days=1):
        return False, float("nan")
    return on, (float(nv[-1]) if len(nv) and not np.isnan(nv[-1]) else float("nan"))


def classify2(bars: pd.DataFrame, tf_min: int, oi: pd.DataFrame | None = None):
    """bars: index=종가시각 DataFrame[open,high,low,close]
    → (regime Series, conf Series, quadrant Series|None).

    리서치 분류기와 동일 수식. oi(snap_ts/oi 5분 스냅샷)를 주면 연료축(OI 4사분면)까지
    3축 판정: ΔOI↑ 사분면(신규 롱/숏)만 라벨에 투표, ΔOI↓(숏커버·롱청산)는 라벨 불변·
    확신 ×0.85, 데드존 ×0.95, OI 미정의 행은 ×0.8(행 단위 인과 degrade — 연구 규약)."""
    c, h, l = bars["close"], bars["high"], bars["low"]
    up, dn = h.diff(), -l.diff()
    ok = up.notna() & dn.notna()
    pdm = up.where((up > dn) & (up > 0), 0.0).where(ok)
    ndm = dn.where((dn > up) & (dn > 0), 0.0).where(ok)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = _wilder(tr, ADX_N)
    pdi = 100 * _wilder(pdm, ADX_N) / atr
    ndi = 100 * _wilder(ndm, ADX_N) / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    adx = _wilder(dx, ADX_N)
    m = (c.ewm(span=EMA_F, adjust=False, min_periods=EMA_F).mean()
         - c.ewm(span=EMA_S, adjust=False, min_periods=EMA_S).mean()) / atr.replace(0, np.nan)

    bpd = 1440 // tf_min
    qw = min(4000, max(200, 30 * bpd))
    mp = max(2, int(qw * 0.95))
    mid = c.rolling(BB_N, min_periods=BB_N).mean()
    sd = c.rolling(BB_N, min_periods=BB_N).std()
    bbw = (2 * BB_K * sd) / mid.replace(0, np.nan)
    vol_pct = 0.5 * _past_pct(bbw, qw, mp) + 0.5 * _past_pct(atr / c, qw, mp)

    a = np.clip((adx.to_numpy(float) - ADX_LO) / ADX_SCALE, 0, 1)
    mm = m.to_numpy(float)
    t = a * np.tanh(np.abs(mm))
    t = np.where(np.sign((pdi - ndi).to_numpy(float)) == np.sign(mm), t, t * 0.5)
    n = len(bars)
    dir_s = np.zeros((n, 5))
    dir_s[:, 0] = np.where(mm > 0, t, 0.0)
    dir_s[:, 1] = np.where(mm < 0, t, 0.0)
    rest = (1.0 - dir_s[:, 0] - dir_s[:, 1]) / 3.0
    dir_s[:, 2] = dir_s[:, 3] = dir_s[:, 4] = 0
    for i in (2, 3, 4):
        dir_s[:, i] = rest
    v = vol_pct.to_numpy(float)
    muL = np.clip((RAMP_LO[1] - v) / (RAMP_LO[1] - RAMP_LO[0]), 0, 1)
    muH = np.clip((v - RAMP_HI[0]) / (RAMP_HI[1] - RAMP_HI[0]), 0, 1)
    muM = np.clip(1.0 - muL - muH, 0, 1)
    vol_s = np.zeros((n, 5))
    vol_s[:, 2], vol_s[:, 3], vol_s[:, 4] = muL, muM, muH
    valid = ~(np.isnan(adx.to_numpy(float)) | np.isnan(mm) | np.isnan(v))

    # ── 연료축(OI 4사분면) — oi 제공 시 3축, 아니면 2축 ──
    quad = np.zeros(n, dtype=np.int8)
    fuel_defined = np.zeros(n, dtype=bool)
    if oi is not None and len(oi):
        fs = _fuel_series(bars, oi, tf_min, qw, mp)
        quad = fs["quad"]
        fuel_defined = fs["fuel_defined"]
    fuel_votes = fuel_defined & ((quad == 1) | (quad == 2))
    fuel_s = np.zeros((n, 5))
    fuel_s[quad == 1, 0] = 1.0
    fuel_s[quad == 2, 1] = 1.0
    w_present = W_DIR + W_VOL + np.where(fuel_votes, W_FUEL, 0.0)
    scores = (W_DIR * dir_s + W_VOL * vol_s + W_FUEL * fuel_s * fuel_votes[:, None]) \
        / w_present[:, None]
    fuel_mod = np.where(fuel_defined & ((quad == 3) | (quad == 4)), 0.85,
                        np.where(fuel_defined & (quad == 0), 0.95, 1.0))
    degrade = (oi is not None) & ~fuel_defined  # 연료 요청됐으나 그 행에서 미정의

    labels = np.full(n, -1, dtype=np.int8)
    conf = np.full(n, np.nan)
    best_arr = np.argmax(scores, axis=1)
    cur = -1
    for i in range(n):
        if not valid[i]:
            cur = -1
            continue
        b = best_arr[i]
        cur = cur if (cur >= 0 and scores[i, cur] >= scores[i, b] - NEAR_TIE) else b
        labels[i] = cur
        agree = scores[i, cur]
        runner = max(scores[i, s2] for s2 in range(5) if s2 != cur)
        cf = agree * (1 - 0.5 * (runner / agree if agree > 1e-12 else 2.0)) * fuel_mod[i]
        if isinstance(degrade, np.ndarray) and degrade[i]:
            cf *= 0.8
        conf[i] = max(0.0, min(1.0, cf))
    reg = pd.Series([REGIMES[i] if i >= 0 else None for i in labels], index=bars.index)
    quad_s = pd.Series(quad, index=bars.index) if oi is not None else None
    return reg, pd.Series(conf, index=bars.index), quad_s


# ── 라이브 스냅샷 ────────────────────────────────────────────────────────────
def _classify_v2_bars(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Canonical v2.1 production path. OI/funding cannot enter this call graph."""
    feat = compute_v2(bars, V2_FP)
    result = research_classify_v2(feat)
    return result["regime"], result["confidence"]


def _confirmed_price_context(symbol: str, interval: str) -> dict | None:
    """Last confirmed native candle + EMA relation; description only, never a v2 label."""
    bars = _fetch_klines(symbol, interval, days=30)
    if bars is None or len(bars) < 55:
        return None
    close = bars["close"]
    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1]
    if pd.isna(ema20) or pd.isna(ema50):
        return None
    relation = "above" if ema20 > ema50 else "below" if ema20 < ema50 else "flat"
    change = (close.iloc[-1] / close.iloc[-2] - 1) * 100 if close.iloc[-2] else None
    return {"tf": interval, "kind": "confirmed_context", "relation": relation,
            "ema20": round(float(ema20), 4), "ema50": round(float(ema50), 4),
            "bar_change": None if change is None else round(float(change), 2),
            "ts": bars.index[-1].strftime("%m-%d %H:%M")}


def live(symbol: str = "BTCUSDT") -> dict:
    """REGIME v2.1 live payload: one canonical 15m Core plus 1h/4h context."""
    now = time.time()
    hit = _live_cache.get(symbol)
    if hit is not None and now - hit["at"] < _TTL:
        return hit["data"]

    bars = _fetch_klines(symbol, "15m", days=60)
    if bars is None or len(bars) < V2_FP.q_window + 100:
        return {"available": False, "error": "REGIME v2.1용 15분 확정봉이 부족합니다"}
    reg, conf = _classify_v2_bars(bars)
    okm = reg.notna() & conf.notna()
    if not okm.any():
        return {"available": False, "error": "REGIME v2.1 burn-in 이후 유효 라벨이 없습니다"}

    r = str(reg[okm].iloc[-1])
    cf = float(conf[okm].iloc[-1])
    hist = conf[okm].to_numpy(float)
    hist30 = hist[-V2_FP.q_window:]
    pct = float((hist30 < cf).mean()) if len(hist30) > 100 else None
    grade = None if pct is None else ("높음" if pct >= 0.7 else "보통" if pct >= 0.3 else "낮음")
    rr, cc = reg[okm].iloc[-192:], conf[okm].iloc[-192:]
    idxmap = {name: i for i, name in enumerate(REGIMES)}
    ribbon = {"cells": [[idxmap.get(str(a), -1), round(float(b), 2)]
                        for a, b in zip(rr, cc)],
              "hours": round(len(rr) * 15 / 60)}
    core = {"tf": "15m", "regime": r, "name": NAME[r], "emoji": EMOJI[r],
            "conf": round(cf, 2), "grade": grade,
            "top_pct": None if pct is None else max(1, round((1 - pct) * 100)),
            "ts": reg[okm].index[-1].strftime("%m-%d %H:%M"), "core": True}
    contexts = [c for c in (_confirmed_price_context(symbol, "1h"),
                            _confirmed_price_context(symbol, "4h")) if c is not None]
    verdict = (f"15분 기준 레짐은 {NAME[r]}입니다. 1시간·4시간 값은 직전 확정봉의 "
               "가격 맥락이며 진입 신호가 아닙니다.")
    basis = ("REGIME v2.1 · 15분 가격 방향·변동성 2축 · OI·펀딩 미사용 · "
             "상태 전환 1봉 확인")
    oi_context_quad = _oi_context_quad(bars, _cached_oi(symbol))
    data = {"available": True, "symbol": symbol, "version": "2.1",
            "tfs": [core], "contexts": contexts, "verdict": verdict, "warn": None,
            "ribbons": {"15m": ribbon}, "ribbon": ribbon, "quad": None,
            "oi_context_quad": oi_context_quad,
            "basis": basis, "stability_delay_bars_vs_v17": 2,
            "supported_chart_minutes": list(V2_CORE_MINUTES_BY_CHART),
            "core_minutes_by_chart": V2_CORE_MINUTES_BY_CHART,
            "oi_separate": True}
    _live_cache[symbol] = {"at": now, "data": data}
    return data


# ── 레짐별 성과 ──────────────────────────────────────────────────────────────
def _norm_sym(s) -> str | None:
    if not s:
        return None
    return str(s).upper().replace("/", "").replace("_", "").split(":")[0] or None


def _labels_for(symbol: str, since: pd.Timestamp):
    """symbol의 canonical v2.1 15m 라벨(since-45d부터). 1시간 캐시."""
    with _lock:
        ent = _labels_cache.get(symbol)
        if ent and time.time() - ent["at"] < 3600 and (ent["s"] is None or ent["since"] <= since):
            return ent["s"]
    start = since - pd.Timedelta(days=45)
    floor = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=365 * 3)  # 폭주 방지 상한 3년
    start = max(start, floor)
    bars = _fetch_klines(symbol, "15m", start_ms=int(start.timestamp() * 1000))
    lab = None
    if bars is not None and len(bars) > 3500:
        # 운영과 성과가 같은 v2.1 경로를 사용한다. OI/funding은 이 호출 그래프에 없다.
        reg, _ = _classify_v2_bars(bars)
        lab = reg
    with _lock:
        _labels_cache[symbol] = {"at": time.time(), "since": start, "s": lab}
    return lab


def _trade_ts(t: dict):
    v = t.get("opened_at") or t.get("closed_at")
    if not v:
        return None, None
    try:
        ts = pd.Timestamp(v)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        return ts, ("진입" if t.get("opened_at") else "청산")
    except (ValueError, TypeError):
        return None, None


def perf(trades: list) -> dict:
    """enrich된 거래 리스트 → 레짐별 {거래수·승률·합손익·평균R}. 진입 시각 기준."""
    agg: dict = {}
    unmatched = 0
    used_exit_ts = 0
    for t in trades or []:
        sym = _norm_sym(t.get("symbol"))
        ts, basis = _trade_ts(t)
        if not sym or ts is None:
            unmatched += 1
            continue
        lab = _labels_for(sym, ts)
        if lab is None:
            unmatched += 1
            continue
        i = lab.index.searchsorted(ts, side="right") - 1
        if i < 0 or (ts - lab.index[i]) > pd.Timedelta(minutes=30):
            unmatched += 1
            continue
        reg = lab.iloc[i]
        if reg is None or (isinstance(reg, float) and pd.isna(reg)):
            unmatched += 1
            continue
        if basis == "청산":
            used_exit_ts += 1
        a = agg.setdefault(reg, {"n": 0, "pnl": 0.0, "wins": 0, "rs": []})
        pnl = float(t.get("pnl") or 0.0)
        a["n"] += 1
        a["pnl"] += pnl
        a["wins"] += 1 if pnl > 0 else 0
        if t.get("r") is not None:
            a["rs"].append(float(t["r"]))
    rows = []
    for reg in REGIMES:
        if reg not in agg:
            continue
        a = agg[reg]
        rows.append({"regime": reg, "name": NAME[reg], "emoji": EMOJI[reg],
                     "n": a["n"], "pnl": round(a["pnl"], 2),
                     "win_rate": round(a["wins"] / a["n"], 2),
                     "avg_r": round(sum(a["rs"]) / len(a["rs"]), 2) if a["rs"] else None,
                     "n_r": len(a["rs"])})
    note = ("REGIME v2.1 · 15분 기준 · 진입 시각 레짐 매칭 · Binance 무기한 미지원 "
            "심볼 제외 · R은 SL 입력 거래만")
    if used_exit_ts:
        note += f" · 진입 시각 누락으로 청산 시각 대체 {used_exit_ts}건"
    return {"rows": rows, "unmatched": unmatched, "note": note}
