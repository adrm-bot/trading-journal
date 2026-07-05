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


TF_MIN = {"15m": 15, "1h": 60, "4h": 240}


def fetch_klines(symbol: str, interval: str = "15m", days: int = 60) -> pd.DataFrame:
    tf = TF_MIN[interval]
    rows = []
    end = None
    need = days * (1440 // tf)
    while len(rows) < need:
        params = dict(symbol=symbol, interval=interval, limit=1500)
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
    k["ts"] = pd.to_datetime(k["open_time"], unit="ms", utc=True) + pd.Timedelta(minutes=tf)
    # drop the in-progress candle against Binance SERVER time (a fast local clock near a
    # boundary would otherwise admit an unfinished candle — labels are close-stamped)
    server_now = pd.to_datetime(_get("/fapi/v1/time")["serverTime"], unit="ms", utc=True)
    k = k[k["ts"] <= server_now]
    k = k.set_index("ts")[["open", "high", "low", "close", "volume"]]
    # complete TF grid, exactly like build_dataset.grid_klines — otherwise every
    # shift/rolling downstream becomes row-based instead of time-based across REST gaps
    grid = pd.date_range(k.index[0], k.index[-1], freq=f"{tf}min", tz="UTC")
    k = k.reindex(grid)
    k.index.name = "ts"
    return k


def fp_for(tf_minutes: int) -> features.FeatureParams:
    """Frozen windows are WALL-CLOCK durations; bar counts scale with the timeframe."""
    bpd = 1440 // tf_minutes
    return features.FeatureParams(q_window=30 * bpd, q_min_periods_fuel=20 * bpd,
                                  oi_lookback=max(1, 120 // tf_minutes), burn_in=35 * bpd)


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


def build_live_frame(symbol: str, interval: str = "15m", days: int = 60,
                     m: pd.DataFrame | None = None,
                     fu: pd.DataFrame | None = None) -> pd.DataFrame:
    k = fetch_klines(symbol, interval, days)
    m = fetch_oi(symbol) if m is None else m.copy()
    fu = fetch_funding(symbol) if fu is None else fu.copy()
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
    df["oi_fresh"] = df["oi"].notna() & ~df["bar_missing"]  # same rule as build_dataset
    return df


NAME = {"TREND_UP": "상승추세", "TREND_DOWN": "하락추세", "SQUEEZE": "수렴(돌파대기)",
        "RANGE": "박스권", "CHOP": "난장판(관망)"}
EMOJI = {"TREND_UP": "🟢", "TREND_DOWN": "🔴", "SQUEEZE": "🔵", "RANGE": "⚪", "CHOP": "🟠"}
SUPER = {"TREND_UP": "UP", "TREND_DOWN": "DOWN"}


def grade(conf_series: pd.Series, conf: float, bars_per_day: int = 96) -> str:
    """절대값(상한 ~0.6) 대신 최근 30일 자기 분위 기반 등급 — 백분위 명시(절대값 병기).

    분위 창은 문구('최근 30일')와 일치하도록 마지막 30일로 슬라이스 — 페치 구간 전체를
    쓰면 TF마다 25~95일로 창이 달라져 '상위 x%'의 기준 모집단이 문구와 어긋남."""
    hist = conf_series.dropna().to_numpy()[-30 * bars_per_day:]
    if len(hist) < 100:
        return f"({conf:.2f})"
    pct = (hist < conf).mean()
    word = "높음 ●●●" if pct >= 0.7 else "보통 ●●○" if pct >= 0.3 else "낮음 ●○○"
    top = max(1, round(100 - pct * 100))  # 자기 자신 포함 강부등호라 0%는 수학적 불가 표기
    return f"{word} · 상위 {top}% (절대 {conf:.2f})"


def drift_state(close: pd.Series, bars_per_day: int) -> tuple[bool, float]:
    """표류(드리프트) 태그 — 표시층 전용, 분류기 불변.

    게이트(4년 BTC 15m 캘리브, results/display_calib.json): on |24h 순이동|≥2% AND
    ER(24h)≥0.10 / off <1.5% or <0.08 (히스테리시스 — 비추세 봉 23.5% 발화, 전환수 −39%).
    정직성: '지난 24h' 과거 기술 — 방향 지속 엣지 없음(표류-on 코호트 실측은 calib JSON).
    Pine v1.6과 동일 수식. 마지막 창에 시간 갭이 있으면(24h≠bars_per_day봉) 미발화 처리.
    """
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
    # 갭 가드: '지난 24h'라는 표기가 참이려면 마지막 창이 정확히 24시간이어야 함
    if len(close) > bars_per_day and \
            close.index[-1] - close.index[-1 - bars_per_day] != pd.Timedelta(days=1):
        return False, float("nan")
    return on, (float(nv[-1]) if len(nv) and not np.isnan(nv[-1]) else float("nan"))


def classify_tf(symbol: str, interval: str, days: int, m, fu):
    df = build_live_frame(symbol, interval, days, m, fu)
    fp = fp_for(TF_MIN[interval])
    res = classifier.classify(features.compute(df, fp))
    res["close"] = df["close"]  # 표시층(드리프트 태그) 계산용 — 분류기 출력 계약 밖
    ok = res["regime"].notna()
    if not ok.any():
        return None
    return res.loc[ok]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows cp949 console
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv[1:]
    symbol = args[0] if args else "BTCUSDT"
    m = fetch_oi(symbol)
    fu = fetch_funding(symbol)

    tracks = {}
    for interval, days in [("15m", 60), ("1h", 80), ("4h", 130)]:
        tracks[interval] = classify_tf(symbol, interval, days, m, fu)
    base = tracks["15m"]
    if base is None:
        sys.exit("분류 가능한 캔들이 없습니다 (데이터 부족)")

    # 표류(드리프트) 태그 — 캘리브 검증 TF(15m·1h)만
    drift = {}
    for interval in ("15m", "1h"):
        t = tracks[interval]
        if t is not None:
            drift[interval] = drift_state(t["close"], 1440 // TF_MIN[interval])

    last = base.iloc[-1]
    ts = base.index[-1]
    regime, conf = last["regime"], last["confidence"]
    day = base.iloc[-96:]
    changes = day["regime"][day["regime"] != day["regime"].shift()]

    def tag_for(interval, reg):
        on, val = drift.get(interval, (False, float("nan")))
        if on and reg in ("RANGE", "SQUEEZE") and not np.isnan(val):
            return f" · 표류{val * 100:+.1f}%/24h"
        return ""

    if as_json:
        import json
        out = {"schema": 1, "symbol": symbol, "ts": ts.isoformat(), "tfs": {}}
        for interval in ("15m", "1h", "4h"):
            t = tracks[interval]
            if t is None:
                out["tfs"][interval] = None  # 트랙 결측을 조용히 빼지 않고 명시
                continue
            r = t.iloc[-1]
            cell = {"regime": r["regime"], "confidence": round(float(r["confidence"]), 4),
                    "ts": t.index[-1].isoformat()}
            if interval in drift:  # 캘리브된 TF만 드리프트 필드 포함 — '미측정'과 '표류 아님' 구분
                d_on, d_val = drift[interval]
                cell["drift_on"] = bool(d_on and r["regime"] in ("RANGE", "SQUEEZE"))
                cell["drift_24h_pct"] = None if np.isnan(d_val) else round(d_val * 100, 2)
            out["tfs"][interval] = cell
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    print(f"=== {symbol} 레짐 스냅샷 (마지막 완결 15m 캔들) ===")
    print(f"시각(UTC)   : {ts:%Y-%m-%d %H:%M}")
    print(f"지금 시장   : {EMOJI[regime]} {NAME[regime]}{tag_for('15m', regime)}")
    print(f"판단 확신   : {grade(base['confidence'], conf, 96)} — 최근 30일 나 자신 대비")
    print(f"데이터      : {'정상 (OI 포함)' if last['fuel_available'] else 'OI 끊김 → 보수 모드'}")
    print(f"24h 흐름    : {' → '.join(NAME[c] for c in changes.tolist()) if len(changes) > 1 else '변화 없음'}")
    print()
    print("멀티 타임프레임 합의")
    supers = []
    for interval in ("15m", "1h", "4h"):
        t = tracks[interval]
        if t is None:
            print(f"  {interval:4s}: 데이터 부족")
            continue
        r = t.iloc[-1]
        supers.append(SUPER.get(r["regime"], "NT"))
        print(f"  {interval:4s}: {EMOJI[r['regime']]} {NAME[r['regime']]:10s} "
              f"확신 {grade(t['confidence'], r['confidence'], 1440 // TF_MIN[interval])}"
              f"{tag_for(interval, r['regime'])}")
    if len(supers) == 3:
        if len(set(supers)) == 1 and supers[0] != "NT":
            verdict = f"세 TF 모두 {'상승' if supers[0] == 'UP' else '하락'} 방향 — 강한 합의"
        elif len(set(supers)) == 1:
            verdict = "세 TF 모두 비추세 — 방향 베팅 근거 없음"
        elif supers[1] == supers[2] and supers[1] != "NT":
            d = "상승" if supers[1] == "UP" else "하락"
            verdict = f"상위(1h·4h) {d} 일치 — 방향 우세, 15m은 진입 타이밍 대기"
        elif supers[0] == supers[1] and supers[0] != "NT":
            verdict = "단기(15m·1h) 일치, 4h 불일치 — 부분 합의, 사이즈 보수"
        else:
            verdict = "혼조 — TF 간 성격 불일치, 보수적으로"
        print(f"  판정: {verdict}")
    print()
    # 플레이북 — 충돌/표류 시 레인지 페이드 문구 대체 (표시층, Pine v1.6 동일 규칙)
    # 근거(continuation_study): HTF추세×LTF박스 전방 share_up 0.485~0.527 — 방향 조언 금지,
    # 전달할 것은 '박스 레벨 신뢰 낮음' 하나.
    drift15 = drift.get("15m", (False, float("nan")))[0]
    htf_trend = any(tracks[i] is not None and tracks[i].iloc[-1]["regime"] in ("TREND_UP", "TREND_DOWN")
                    for i in ("1h", "4h"))
    conflict = regime in ("RANGE", "SQUEEZE") and (drift15 or htf_trend)
    pb = REGIME_PLAYBOOK[regime]
    print(f"플레이북 [{NAME[regime]}]{' — ⚠ 충돌/표류 구간' if conflict else ''}")
    if conflict:
        print("  지금 할 것 : 박스 레벨 신뢰 낮음 — 페이드 보류 · 트리거 확정 후 · 사이즈 축소")
        print("  하지 말 것 : 이동 중인 박스 경계 역매매, 물타기")
        print("  근거       : 상위TF 추세 또는 24h 표류 — 어느 방향도 엣지 없음(전방 0.49~0.53),")
        print("               박스 페이드의 전제(레벨 정상성)만 깨진 상태. 방향 신호 아님.")
    else:
        print(f"  지금 할 것 : {pb['primary']}")
        print(f"  보조       : {', '.join(pb['secondary'])}")
        print(f"  하지 말 것 : {', '.join(pb['avoid'])}")
    hist = base["confidence"].to_numpy()[-2880:]
    if conf < np.nanquantile(hist, 0.2):
        print("\n⚠ 전환 경고: 확신이 최근 30일 하위 20% — 시장 성격이 바뀌는 중일 수 있음. 사이즈 축소.")


if __name__ == "__main__":
    main()
