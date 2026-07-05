#!/usr/bin/env python3
"""regime.py — 시장 레짐 카드 + '레짐별 내 성과' (research/regime 검증 분류기의 경량 포팅).

같은 레포 research/regime의 5-상태 분류기에서 **가격 기반 2축(방향·변동성)** 만 포팅했다.
근거: 리서치에서 OI 연료축의 타이밍 기여가 순환 시간이동 널과 구별 불가(p=1.0)로 판정돼,
클라우드(Render)에 OI 파이프라인 없이도 결과 손실이 없는 근사다. 파라미터는 리서치 동결값
(재튜닝 금지). 데이터: Binance USDT-M 공개 REST(무키·유저 크리덴셜 불필요).

레짐별 성과는 **진입 시각(opened_at) 기준** 매칭(없으면 청산 시각 폴백) — 바이낸스 무기한에
없는 심볼의 거래는 '미매칭'으로 정직하게 따로 센다. 모든 실패는 available:false류로 강등되고
앱은 계속 뜬다(시장 카드들과 동일 규약).
"""
import logging
import threading
import time

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("regime")

FAPI = "https://fapi.binance.com"
REGIMES = ("TREND_UP", "TREND_DOWN", "SQUEEZE", "RANGE", "CHOP")
NAME = {"TREND_UP": "상승추세", "TREND_DOWN": "하락추세", "SQUEEZE": "수렴(돌파대기)",
        "RANGE": "박스권", "CHOP": "난장판(관망)"}
EMOJI = {"TREND_UP": "🟢", "TREND_DOWN": "🔴", "SQUEEZE": "🔵", "RANGE": "⚪", "CHOP": "🟠"}
SUPER = {"TREND_UP": "UP", "TREND_DOWN": "DOWN"}

# 리서치 동결 파라미터 (창은 벽시계 기준, TF별 봉수 환산)
ADX_N, EMA_F, EMA_S, BB_N, BB_K = 14, 20, 50, 20, 2.0
ADX_LO, ADX_SCALE = 20.0, 15.0
W_DIR, W_VOL, W_FUEL, NEAR_TIE = 0.5, 0.3, 0.2, 0.02
RAMP_LO, RAMP_HI = (0.15, 0.25), (0.75, 0.85)
DEAD_Q = 0.25
QUAD_TEXT = {1: "🟩 신규 롱 유입 — 추세 연료(라벨 투표)",
             2: "🟥 신규 숏 유입 — 추세 연료(라벨 투표)",
             3: "↩ 숏 커버 — 라벨 불변·확신 −15%",
             4: "↩ 롱 청산 — 라벨 불변·확신 −15%",
             0: "— 데드존(유의미한 ΔOI 없음)"}

_TTL = 300
_live_cache = {"at": 0.0, "data": None}
_labels_cache: dict = {}   # sym -> {"at": ts, "since": Timestamp, "s": Series|None}
_lock = threading.Lock()


# ── 데이터 ───────────────────────────────────────────────────────────────────
def _grid(interval: str) -> int:
    return {"15m": 15, "1h": 60, "4h": 240}[interval]


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
    return m.drop_duplicates("snap_ts").sort_values("snap_ts")[["snap_ts", "oi"]]


# ── 2축 분류 (리서치 B2와 동일 수식) ─────────────────────────────────────────
def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _past_pct(s: pd.Series, w: int, mp: int) -> pd.Series:
    r = s.rolling(w, min_periods=mp).rank(pct=True)
    c = s.rolling(w, min_periods=mp).count()
    return ((r * c - 1) / (c - 1)).where(c > 1)


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
        k = max(1, 120 // tf_min)  # ΔOI lookback = 2시간
        left = pd.DataFrame({"ts": bars.index.astype("datetime64[ns, UTC]")})
        right = oi.rename(columns={"snap_ts": "ts"}).copy()
        right["ts"] = right["ts"].astype("datetime64[ns, UTC]")
        joined = pd.merge_asof(left, right.sort_values("ts"), on="ts",
                               direction="backward",
                               tolerance=pd.Timedelta(minutes=25)).set_index("ts")["oi"]
        joined.index = bars.index
        fresh = joined.notna()
        doi = (joined / joined.shift(k) - 1.0).where(fresh & fresh.shift(k, fill_value=False))
        dpr = c / c.shift(k) - 1.0
        mp_fuel = max(2, min(20 * bpd, int(qw * 0.95)))
        dz_doi = doi.abs().rolling(qw, min_periods=mp_fuel).quantile(DEAD_Q).shift(1)
        dz_dpr = dpr.abs().rolling(qw, min_periods=mp).quantile(DEAD_Q).shift(1)
        d, p = doi.to_numpy(float), dpr.to_numpy(float)
        zd, zp = dz_doi.to_numpy(float), dz_dpr.to_numpy(float)
        on = ~np.isnan(d) & ~np.isnan(zd) & ~np.isnan(zp) & (np.abs(d) >= zd) & (np.abs(p) >= zp)
        quad = np.select([on & (d > 0) & (p > 0), on & (d > 0) & (p < 0),
                          on & (d < 0) & (p > 0), on & (d < 0) & (p < 0)], [1, 2, 3, 4], 0)
        fuel_defined = ~np.isnan(d) & ~np.isnan(zd)
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
def live(symbol: str = "BTCUSDT") -> dict:
    now = time.time()
    if _live_cache["data"] is not None and now - _live_cache["at"] < _TTL:
        return _live_cache["data"]
    tfs, supers = [], []
    warn = None
    quad_info = None
    oi = _fetch_oi(symbol)
    for interval, days in [("15m", 60), ("1h", 80), ("4h", 130)]:
        bars = _fetch_klines(symbol, interval, days=days)
        if bars is None or len(bars) < 300:
            continue
        reg, conf, quad = classify2(bars, _grid(interval), oi=oi)
        okm = reg.notna()
        if not okm.any():
            continue
        if interval == "15m" and quad is not None:
            q = int(quad[okm].iloc[-1])
            quad_info = {"code": q, "text": QUAD_TEXT.get(q, "—"),
                         "oi": True}
        r, cf = reg[okm].iloc[-1], float(conf[okm].iloc[-1])
        hist = conf[okm].to_numpy(float)
        pct = float((hist < cf).mean()) if len(hist) > 100 else None
        grade = None if pct is None else ("높음" if pct >= 0.7 else "보통" if pct >= 0.3 else "낮음")
        supers.append(SUPER.get(r, "NT"))
        tfs.append({"tf": interval, "regime": r, "name": NAME[r], "emoji": EMOJI[r],
                    "conf": round(cf, 2), "grade": grade,
                    "ts": reg[okm].index[-1].strftime("%m-%d %H:%M")})
        if interval == "15m" and len(hist) > 500 and cf < float(np.quantile(hist[-2880:], 0.2)):
            warn = "확신이 최근 30일 하위 20% — 성격 전환 가능성, 사이즈 축소"
    if not tfs:
        return {"available": False, "error": "레짐 데이터 조회 실패 (Binance 공개 API)"}
    if len(supers) == 3:
        if len(set(supers)) == 1 and supers[0] != "NT":
            verdict = f"세 TF 모두 {'상승' if supers[0] == 'UP' else '하락'} 방향 — 강한 합의"
        elif len(set(supers)) == 1:
            verdict = "세 TF 모두 비추세 — 방향 베팅 근거 없음"
        elif supers[1] == supers[2] and supers[1] != "NT":
            verdict = f"상위(1h·4h) {'상승' if supers[1] == 'UP' else '하락'} 일치 — 방향 우세, 15m은 진입 타이밍 대기"
        elif supers[0] == supers[1] and supers[0] != "NT":
            verdict = "단기(15m·1h) 일치, 4h 불일치 — 부분 합의, 사이즈 보수"
        else:
            verdict = "혼조 — TF 간 성격 불일치, 보수적으로"
    else:
        verdict = "일부 TF 데이터 부족"
    if quad_info is None:
        quad_info = {"code": None, "text": "OI 미제공 심볼 — 가격 2축으로 판정", "oi": False}
    data = {"available": True, "symbol": symbol, "tfs": tfs, "verdict": verdict, "warn": warn,
            "quad": quad_info,
            "basis": ("라이브 3축(방향·변동성·OI 사분면)" if quad_info["oi"] else
                      "가격 기반 2축(방향·변동성)")
                     + " — 사분면은 ΔOI↑만 라벨 투표, ΔOI↓는 확신 삭감(연구 반영)"}
    _live_cache.update(at=now, data=data)
    return data


# ── 레짐별 성과 ──────────────────────────────────────────────────────────────
def _norm_sym(s) -> str | None:
    if not s:
        return None
    return str(s).upper().replace("/", "").replace("_", "").split(":")[0] or None


def _labels_for(symbol: str, since: pd.Timestamp):
    """symbol의 15m 레짐 라벨(since-45d부터). 1시간 캐시. 미상장 심볼은 None 캐시."""
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
        # 성과 라벨은 가격 2축 — REST 과거 OI가 30일 한도라 불가(연구상 무손실 근사, p=1.0)
        reg, _, _ = classify2(bars, 15)
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
    note = "진입 시각 레짐 기준 · 바이낸스 무기한 미상장 심볼은 미매칭 · R은 SL 기입 거래만"
    if used_exit_ts:
        note += f" · 진입시각 없어 청산시각 사용 {used_exit_ts}건"
    return {"rows": rows, "unmatched": unmatched, "note": note}
