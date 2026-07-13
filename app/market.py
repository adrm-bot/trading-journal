"""market.py — 공개 시장 컨텍스트 (인증 불필요). 서버측 캐시로 외부 API 보호.

대시보드 상단 스트립용: BTC 추세 · ETH · 공포탐욕지수 · BTC 도미넌스.
모든 외부호출은 개별 try로 감싸 일부 실패해도 나머지는 살린다(대시보드 절대 안 죽임).
"""
import json
import logging
import time
import urllib.request

import ccxt

from . import tvremix

logger = logging.getLogger("app.market")

_CACHE = {"at": 0.0, "data": None}
_TTL = 300  # 5분


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "trading-journal/1.0"})
    with urllib.request.urlopen(req, timeout=6) as r:  # noqa: S310 (고정 https URL)
        return json.loads(r.read().decode())


def _ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _trend(closes):
    """EMA50/EMA200 정·역배열로 추세 판정. 200봉 미만이면 (None, None, None)."""
    if len(closes) < 200:
        return None, None, None
    e50, e200 = _ema(closes, 50), _ema(closes, 200)
    last = closes[-1]
    if last > e50 > e200:
        t = "up"
    elif last < e50 < e200:
        t = "down"
    else:
        t = "side"
    return t, e50, e200


def _trailing_change(closes, bars):
    """마지막 값 기준 bars개 전 대비 변화율. 표본이 모자라면 추정하지 않는다."""
    if not closes or len(closes) <= bars or not closes[-1 - bars]:
        return None
    return round((closes[-1] / closes[-1 - bars] - 1) * 100, 2)


# 멀티 타임프레임(BTC): 단기 1h · 중기 4h · 주추세 1d 정렬을 한눈에
_TFS = ("1h", "4h", "1d")


def _coin(ex, symbol, mtf=False, with_rs=False):
    oh = ex.fetch_ohlcv(symbol, "1d", limit=300)  # EMA200 워밍업 위해 일봉 300개
    closes = [c[4] for c in oh if c and c[4]]
    if len(closes) < 10:
        return None
    last = closes[-1]
    chg24 = (last / closes[-2] - 1) * 100 if len(closes) >= 2 else None
    chg7 = (last / closes[-8] - 1) * 100 if len(closes) >= 8 else None
    chg90 = (last / closes[-91] - 1) * 100 if len(closes) >= 91 else None  # 알트시즌(90일) 비교용
    trend, e50, e200 = _trend(closes)  # 일봉 EMA50/EMA200(골든/데드 크로스) = 주추세
    out = {"price": round(last, 2), "trend": trend,
           "chg24": round(chg24, 2) if chg24 is not None else None,
           "chg7": round(chg7, 2) if chg7 is not None else None,
           "chg90": round(chg90, 2) if chg90 is not None else None,
           "ema50": round(e50, 2) if e50 is not None else None,
           "ema200": round(e200, 2) if e200 is not None else None,
           # 최근 30일 종가 스파크(newest-first: F&G·알트시즌과 동일 규약) — 이미 받은 캔들 재사용(추가 호출 0)
           "spark": [round(c, 8) for c in closes[-30:]][::-1] if len(closes) >= 10 else None}
    if with_rs:
        # 상대강도 계산 전용 시계열. _alt_board에서 소비 후 제거하므로 API 응답에는 노출하지 않는다.
        out["_rs_series"] = [[int(c[0]), float(c[4])] for c in oh if c and c[4]][-91:]
    if mtf:  # 1h·4h·1d 각각 EMA50/200 추세 (1d는 이미 받은 캔들 재사용)
        tf = {}
        hourly = None
        for t in _TFS:
            try:
                cl = closes if t == "1d" else [
                    x[4] for x in ex.fetch_ohlcv(symbol, t, limit=300) if x and x[4]]
                if t == "1h":
                    hourly = cl
                tr, a, b = _trend(cl)
                tf[t] = {"trend": tr,
                         "ema50": round(a, 2) if a is not None else None,
                         "ema200": round(b, 2) if b is not None else None}
            except Exception:  # noqa: BLE001
                tf[t] = {"trend": None, "ema50": None, "ema200": None}
        out["tf"] = tf
        # OI 카드의 기간 선택과 가격축을 정확히 맞추기 위한 후행 변화율.
        # 이미 추세 판정용으로 받은 1시간봉을 재사용하므로 외부 호출은 늘지 않는다.
        out["chg_tf"] = {
            "1h": _trailing_change(hourly, 1),
            "4h": _trailing_change(hourly, 4),
            "24h": _trailing_change(hourly, 24) if hourly else out.get("chg24"),
            "7d": _trailing_change(hourly, 24 * 7) if hourly else out.get("chg7"),
        }
    return out


def _capture_profile(alt_series, btc_series, window=60):
    """BTC 상승일/하락일의 알트 포착률과 비대칭 강도를 계산한다.

    상방 포착률 120 = BTC가 오른 날의 누적 상승폭 대비 알트가 1.2배 움직였다는 뜻이다.
    하방 포착률 80 = BTC가 내린 날의 누적 하락폭 대비 알트 하락폭이 0.8배였다는 뜻이다.
    강도 = 상방 포착률 - 하방 포착률. 높을수록 상승 참여와 하락 방어의 조합이 낫다.
    """
    try:
        alt = {int(t): float(v) for t, v in (alt_series or []) if v}
        btc = {int(t): float(v) for t, v in (btc_series or []) if v}
    except (TypeError, ValueError):
        return None
    stamps = sorted(set(alt) & set(btc))
    if len(stamps) < window + 1:
        return None
    stamps = stamps[-(window + 1):]
    up_alt, up_btc, down_alt, down_btc = 0.0, 0.0, 0.0, 0.0
    up_n = down_n = 0
    for prev, cur in zip(stamps, stamps[1:]):
        if not alt[prev] or not btc[prev]:
            continue
        ar = alt[cur] / alt[prev] - 1
        br = btc[cur] / btc[prev] - 1
        if br > 0:
            up_alt += ar
            up_btc += br
            up_n += 1
        elif br < 0:
            down_alt += ar
            down_btc += br
            down_n += 1
    if up_n < 3 or down_n < 3 or not up_btc or not down_btc:
        return None
    up_capture = up_alt / up_btc * 100
    down_capture = down_alt / down_btc * 100
    return {
        "up_capture_60d": round(up_capture, 1),
        "down_capture_60d": round(down_capture, 1),
        "rs_score": round(up_capture - down_capture, 1),
        "sample_up": up_n,
        "sample_down": down_n,
        "window_days": min(window, len(stamps) - 1),
    }


def _ratio_trend(a, b):
    """두 종가 시리즈 비율(a/b)의 EMA50/200 추세 — 도미넌스 방향 가격기반 프록시."""
    n = min(len(a), len(b))
    if n < 200:
        return None
    ratio = [a[-n + i] / b[-n + i] for i in range(n)]
    return _trend(ratio)[0]


def _invert(t):
    return {"up": "down", "down": "up", "side": "side"}.get(t)


def _closes(ex, symbol, tf):
    return [c[4] for c in ex.fetch_ohlcv(symbol, tf, limit=300) if c and c[4]]


def _dom_proxy(ex):
    """BTC.D·USDT.D의 1H·4H·1D 추세 = 가격기반 추정(무료 API에 도미넌스 시계열 없음).
    BTC.D ≈ BTC/ETH 비율 추세(BTC가 알트 대장 대비 점유율↑/↓),
    USDT.D ≈ 시장(BTC+ETH 지수) 추세의 역(시장↓ = 현금/스테이블 점유율↑ = 리스크오프)."""
    btcd, usdtd = {}, {}
    for tf in _TFS:
        try:
            bc, ec = _closes(ex, "BTC/USDT", tf), _closes(ex, "ETH/USDT", tf)
            btcd[tf] = _ratio_trend(bc, ec)
            n = min(len(bc), len(ec))
            mkt = [bc[-n + i] / bc[-n] + ec[-n + i] / ec[-n] for i in range(n)] if n >= 200 else []
            usdtd[tf] = _invert(_trend(mkt)[0]) if mkt else None
        except Exception:  # noqa: BLE001
            btcd[tf], usdtd[tf] = None, None
    return btcd, usdtd


# TVRemix 인터벌 매핑(우리 1d → TradingView 1D)
_TV_TF = {"1h": "1h", "4h": "4h", "1d": "1D"}


def _tv_market():
    """TVRemix 실데이터: 도미넌스(BTC.D/USDT.D) 멀티TF EMA50/200 추세 + 알트 점유율 90일 시계열.
    추세=프록시 아닌 실제 CRYPTOCAP 시계열. altshare=TOTAL2/TOTAL(알트 시총 점유율) 최근 90일.
    키 없거나 도미넌스 추세가 전부 실패하면 None(호출부가 가격기반 프록시로 폴백)."""
    if not tvremix.enabled():
        return None
    btcd, usdtd = {}, {}
    ok = False
    for tf, iv in _TV_TF.items():
        bd = _trend(tvremix.closes("CRYPTOCAP:BTC.D", iv, 300))[0]
        ud = _trend(tvremix.closes("CRYPTOCAP:USDT.D", iv, 300))[0]
        if bd is not None or ud is not None:
            ok = True
        btcd[tf], usdtd[tf] = bd, ud
    if not ok:
        return None  # 사실상 실패 → 폴백
    out = {"btcd_tf": btcd, "usdtd_tf": usdtd, "altshare": None}
    t2 = tvremix.closes("CRYPTOCAP:TOTAL2", "1D", 120)  # 알트(BTC 제외) 시총
    tt = tvremix.closes("CRYPTOCAP:TOTAL", "1D", 120)   # 전체 시총
    n = min(len(t2), len(tt))
    if n >= 30:  # 알트 점유율 = TOTAL2/TOTAL × 100, 최근 90일(newest-first: spark 규약)
        share = [round(t2[-n + i] / tt[-n + i] * 100, 2) for i in range(n)]
        out["altshare"] = share[-90:][::-1]
        # TOTAL/TOTAL2 30일 가격 추이 — 이미 받은 시계열 재사용(실데이터, 키 없으면 미제공=정직)
        out["total_spark"] = [round(v) for v in tt[-30:]][::-1]
        out["total2_spark"] = [round(v) for v in t2[-30:]][::-1]
    return out


# 상대강도 비교군 — 섹터 다양성 확보(BTC 대비 7일 상대수익률로 강약 순위).
# 심볼은 Bybit/Binance 현물 /USDT 기준. 거래소에 없거나(신규·미상장) 조회 실패하는
# 심볼은 _alt_board가 개별적으로 건너뜀 → 전체 보드를 죽이지 않는다(빠진 심볼은 조용히 제외).
# ETH는 첫 항목 유지(대장 스냅샷 eth로 별도 사용). 순서 무관 — 최종 vs_btc_7d로 재정렬.
ALTS = [
    # 대형 시총 우선 표본(스테이블·랩드 자산 제외). 화면은 시총순이 아니라 상대강도순이다.
    "ETH", "BNB", "XRP", "SOL", "TRX", "DOGE", "ADA", "BCH", "LINK", "AVAX",
    "TON", "SUI", "XLM", "HBAR", "DOT", "LTC", "UNI", "NEAR", "ICP", "APT",
    "AAVE", "ATOM", "FIL", "KAS",
    # 섹터 대표 보강 — 상대강도 화면에서는 섹터 라벨을 붙이지 않고 동일 기준으로 정렬한다.
    "ARB", "OP", "MNT",                    # L2
    "FET", "RENDER", "WLD", "TAO",        # AI · DePIN
    "ONDO",                                  # RWA
    "ENA", "PENDLE", "LDO",                # DeFi · 수익률
    "PYTH",                                  # 오라클
    "IMX",                                   # 게임
    "PEPE",                                  # 밈
    "HYPE", "DYDX",                         # perp-DEX
]


def _alt_board(ex, btc):
    """ALTS 각각의 BTC 대비 60일 상·하방 포착률 보드 + 7일 초과수익률 + ETH 스냅샷.
    심볼별 조회를 개별 try로 감싼다 — 거래소에 없거나(신규·미상장) 타임아웃 나는 알트
    하나가 전체 보드를 죽이지 않게. 빠진 심볼은 조용히 제외(정직: 없는 데이터는 안 만든다)."""
    btc_chg7 = btc.get("chg7") if btc else None
    btc_series = (btc or {}).get("_rs_series") or []
    eth, board = None, []
    for sym in ALTS:
        try:
            c = _coin(ex, f"{sym}/USDT", with_rs=True)
        except Exception:  # noqa: BLE001 — BadSymbol·타임아웃 등, 개별 스킵
            logger.debug("market 알트 %s 조회 실패 — 보드에서 제외", sym)
            continue
        profile = _capture_profile((c or {}).pop("_rs_series", None), btc_series)
        if sym == "ETH":
            eth = c
        if c and c.get("chg7") is not None and btc_chg7 is not None:
            row = {"sym": sym, "chg24": c.get("chg24"), "chg7": c.get("chg7"),
                   "chg90": c.get("chg90"),
                   "vs_btc_7d": round(c["chg7"] - btc_chg7, 2)}
            if profile:
                row.update(profile)
            board.append(row)
    board.sort(key=lambda b: (b.get("rs_score") is not None,
                              b.get("rs_score") if b.get("rs_score") is not None else b["vs_btc_7d"]),
               reverse=True)
    return eth, board


def _regime(board, btc):
    """알트 breadth로 시장 레짐 판정: BTC 우위 / 알트 우위 / 혼조."""
    n = len(board)
    if not n:
        return None
    scored = [b for b in board if b.get("rs_score") is not None]
    basis_rows = scored or board
    op = sum(1 for b in basis_rows if (b.get("rs_score") if scored else b["vs_btc_7d"]) > 0)
    n_basis = len(basis_rows)
    if op <= n_basis * 0.35:
        regime = "btc"       # 대부분 알트가 BTC보다 약함 → BTC 우위
    elif op >= n_basis * 0.65:
        regime = "alt"       # 대부분 알트가 BTC보다 강함 → 알트 우위
    else:
        regime = "neutral"
    return {"regime": regime, "board": board, "n": n, "universe_n": len(ALTS),
            "universe_basis": "대형 시총 우선 + 섹터 대표 보강",
            "outperform": op, "breadth_n": n_basis,
            "strength_basis": "60일 상방 포착률 - 하방 포착률" if scored else "7일 BTC 초과수익률",
            "btc_chg7": btc.get("chg7"), "btc_trend": btc.get("trend")}


def _altseason(board, btc):
    """알트시즌 지수(0-100) = 최근 90일 BTC 수익률을 초과한 알트 비율.
    표준 Altcoin Season Index(보통 top50) 축약판 — 표본 N=알트수라 방향 참고용."""
    bc90 = btc.get("chg90")
    pts = [b for b in board if b.get("chg90") is not None]
    if bc90 is None or not pts:
        return None
    op = sum(1 for b in pts if b["chg90"] > bc90)
    return {"value": round(op / len(pts) * 100), "n": len(pts)}


def _market_data():
    """리전 차단 회피: Bybit→Binance. BTC·ETH 시세 + 알트 상대강도 보드/레짐 + 도미넌스 프록시 추세."""
    for name in ("bybit", "binance"):
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            btc = _coin(ex, "BTC/USDT", mtf=True, with_rs=True)  # BTC만 1h/4h/1d 멀티TF
            if not btc:
                continue
            eth, board = _alt_board(ex, btc)
            btc.pop("_rs_series", None)
            btcd_tf, usdtd_tf = _dom_proxy(ex)
            return {"btc": btc, "eth": eth, "rs": _regime(board, btc), "src": name,
                    "btcd_tf": btcd_tf, "usdtd_tf": usdtd_tf, "altseason": _altseason(board, btc)}
        except Exception:  # noqa: BLE001
            logger.warning("market 시세 %s 실패", name)
    return {"btc": None, "eth": None, "rs": None, "src": None,
            "btcd_tf": None, "usdtd_tf": None, "altseason": None}


def _fng():
    d = _get_json("https://api.alternative.me/fng/?limit=8")["data"]  # 일별 8일(오늘~7일전)
    cur = d[0]
    return {"value": int(cur["value"]), "label": cur.get("value_classification", ""),
            "history": [int(x["value"]) for x in d]}  # [오늘, 어제, 그제, … 7일전]


def _dominance():
    g = _get_json("https://api.coingecko.com/api/v3/global")["data"]
    mcp = g.get("market_cap_percentage", {}) or {}
    total = (g.get("total_market_cap", {}) or {}).get("usd") or 0
    total_vol = (g.get("total_volume", {}) or {}).get("usd") or 0
    btc_d = mcp.get("btc", 0) or 0
    eth_d = mcp.get("eth", 0) or 0
    usdt_d = mcp.get("usdt", 0) or 0
    usdc_d = mcp.get("usdc", 0) or 0
    others_d = max(0.0, 100 - sum(v for v in mcp.values() if isinstance(v, (int, float))))
    return {"btc": round(btc_d, 1), "eth": round(eth_d, 1), "others": round(others_d, 1),
            "usdt": round(usdt_d, 2), "usdc": round(usdc_d, 2),          # 스테이블 도미넌스
            "mcap_chg": round(g.get("market_cap_change_percentage_24h_usd") or 0, 2),
            "total": total, "total_vol": total_vol,                      # TOTAL 시총·24h 거래대금(정확)
            "total2": total * (1 - btc_d / 100),               # BTC 제외 시총
            "total3": total * (1 - (btc_d + eth_d) / 100),       # BTC+ETH 제외 시총
            "others_usd": total * others_d / 100}                # 상위 제외(알트 잔여)


def _sectors():
    """크립토 섹터(카테고리) 24h 시총변화 — '지금 강한/약한 섹터'. CoinGecko categories."""
    cats = _get_json("https://api.coingecko.com/api/v3/coins/categories")
    # 시총 하한($1B): 초소형·노이즈 카테고리(ERC404·밈 파생 등)가 24h 변동률로 1등 먹는 것 방지 → 의미 있는 섹터만
    rows = [c for c in cats if isinstance(c.get("market_cap_change_24h"), (int, float)) and (c.get("market_cap") or 0) > 1e9]
    rows.sort(key=lambda c: c["market_cap_change_24h"], reverse=True)

    def _row(c):
        ids = c.get("top_3_coins_id") or []  # 섹터 대표 코인(구성 심볼 '대략')
        coins = [str(x).replace("-", " ") for x in ids if x][:3]
        return {"name": c.get("name") or "?", "chg24": round(c["market_cap_change_24h"], 2),
                "vol": c.get("volume_24h"), "coins": coins}  # vol=24h 거래대금(실측)
    return {"top": [_row(c) for c in rows[:8]], "bottom": [_row(c) for c in rows[-5:]],
            "universe_n": len(rows), "basis": "CoinGecko 시총 $1B 이상 카테고리"}


def get_context(force=False):
    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["at"] < _TTL:
        return _CACHE["data"]
    md = _market_data()
    data = {"btc": md["btc"], "eth": md["eth"], "rs": md["rs"], "src": md["src"],
            "altseason": md.get("altseason"),
            "fng": None, "dom": None, "sectors": None, "ts": int(now)}
    for key, fn in (("fng", _fng), ("dom", _dominance), ("sectors", _sectors)):
        try:
            data[key] = fn()
        except Exception:  # noqa: BLE001
            logger.warning("market %s 실패", key)
    real = None
    if tvremix.enabled():
        try:
            real = _tv_market()
        except Exception:  # noqa: BLE001
            logger.warning("market TVRemix 실데이터 실패 → 프록시 폴백")
    if data.get("dom"):  # 도미넌스 멀티TF 추세: TVRemix 실데이터 우선, 실패 시 가격기반 프록시
        data["dom"]["btcd_tf"] = real["btcd_tf"] if real else md.get("btcd_tf")
        data["dom"]["usdtd_tf"] = real["usdtd_tf"] if real else md.get("usdtd_tf")
        data["dom"]["trend_real"] = bool(real)
    if data.get("altseason") and real and real.get("altshare"):  # 알트 점유율 실시계열
        data["altseason"]["spark"] = real["altshare"]
    if data.get("dom") and real:  # TOTAL/TOTAL2 30일 추이(실시계열만 — 프록시로 지어내지 않음)
        data["dom"]["total_spark"] = real.get("total_spark")
        data["dom"]["total2_spark"] = real.get("total2_spark")
    _CACHE["data"], _CACHE["at"] = data, now
    return data
