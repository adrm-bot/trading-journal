"""market.py — 공개 시장 컨텍스트 (인증 불필요). 서버측 캐시로 외부 API 보호.

대시보드 상단 스트립용: BTC 추세 · ETH · 공포탐욕지수 · BTC 도미넌스.
모든 외부호출은 개별 try로 감싸 일부 실패해도 나머지는 살린다(대시보드 절대 안 죽임).
"""
import json
import logging
import time
import urllib.request

import ccxt

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


# 멀티 타임프레임(BTC): 단기 1h · 중기 4h · 주추세 1d 정렬을 한눈에
_TFS = ("1h", "4h", "1d")


def _coin(ex, symbol, mtf=False):
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
           "ema200": round(e200, 2) if e200 is not None else None}
    if mtf:  # 1h·4h·1d 각각 EMA50/200 추세 (1d는 이미 받은 캔들 재사용)
        tf = {}
        for t in _TFS:
            try:
                cl = closes if t == "1d" else [
                    x[4] for x in ex.fetch_ohlcv(symbol, t, limit=300) if x and x[4]]
                tr, a, b = _trend(cl)
                tf[t] = {"trend": tr,
                         "ema50": round(a, 2) if a is not None else None,
                         "ema200": round(b, 2) if b is not None else None}
            except Exception:  # noqa: BLE001
                tf[t] = {"trend": None, "ema50": None, "ema200": None}
        out["tf"] = tf
    return out


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


# 상대강도 비교군(메이저 알트). BTC 대비 7일 상대수익률로 강약 순위.
ALTS = ["ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK"]


def _regime(board, btc):
    """알트 breadth로 시장 레짐 판정: BTC 우위 / 알트 우위 / 혼조."""
    n = len(board)
    if not n:
        return None
    op = sum(1 for b in board if b["vs_btc_7d"] > 0)  # BTC보다 7일 강한 알트 수
    if op <= n * 0.35:
        regime = "btc"       # 대부분 알트가 BTC보다 약함 → BTC 우위
    elif op >= n * 0.65:
        regime = "alt"       # 대부분 알트가 BTC보다 강함 → 알트 우위
    else:
        regime = "neutral"
    return {"regime": regime, "board": board, "n": n, "outperform": op,
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
            btc = _coin(ex, "BTC/USDT", mtf=True)  # BTC만 1h/4h/1d 멀티TF
            if not btc:
                continue
            eth, board = None, []
            for sym in ALTS:
                c = _coin(ex, f"{sym}/USDT")
                if sym == "ETH":
                    eth = c
                if c and c.get("chg7") is not None and btc.get("chg7") is not None:
                    board.append({"sym": sym, "chg24": c.get("chg24"), "chg7": c.get("chg7"),
                                  "chg90": c.get("chg90"),
                                  "vs_btc_7d": round(c["chg7"] - btc["chg7"], 2)})
            board.sort(key=lambda b: b["vs_btc_7d"], reverse=True)
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
    return {"top": [_row(c) for c in rows[:5]], "bottom": [_row(c) for c in rows[-3:]]}


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
    if data.get("dom"):  # 도미넌스 칩에 멀티TF 프록시 추세 주입(가격기반 추정)
        data["dom"]["btcd_tf"] = md.get("btcd_tf")
        data["dom"]["usdtd_tf"] = md.get("usdtd_tf")
    _CACHE["data"], _CACHE["at"] = data, now
    return data
