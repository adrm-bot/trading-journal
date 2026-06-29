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


def _coin(ex, symbol):
    oh = ex.fetch_ohlcv(symbol, "1d", limit=60)
    closes = [c[4] for c in oh if c and c[4]]
    if len(closes) < 10:
        return None
    last = closes[-1]
    chg24 = (last / closes[-2] - 1) * 100 if len(closes) >= 2 else None
    chg7 = (last / closes[-8] - 1) * 100 if len(closes) >= 8 else None
    trend = None
    if len(closes) >= 50:
        e20, e50 = _ema(closes, 20), _ema(closes, 50)
        if last > e20 > e50:
            trend = "up"
        elif last < e20 < e50:
            trend = "down"
        else:
            trend = "side"
    return {"price": round(last, 2), "trend": trend,
            "chg24": round(chg24, 2) if chg24 is not None else None,
            "chg7": round(chg7, 2) if chg7 is not None else None}


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


def _market_data():
    """리전 차단 회피: Bybit→Binance. BTC·ETH 시세 + 알트 상대강도 보드/레짐."""
    for name in ("bybit", "binance"):
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            btc = _coin(ex, "BTC/USDT")
            if not btc:
                continue
            eth, board = None, []
            for sym in ALTS:
                c = _coin(ex, f"{sym}/USDT")
                if sym == "ETH":
                    eth = c
                if c and c.get("chg7") is not None and btc.get("chg7") is not None:
                    board.append({"sym": sym, "chg24": c.get("chg24"), "chg7": c.get("chg7"),
                                  "vs_btc_7d": round(c["chg7"] - btc["chg7"], 2)})
            board.sort(key=lambda b: b["vs_btc_7d"], reverse=True)
            return {"btc": btc, "eth": eth, "rs": _regime(board, btc), "src": name}
        except Exception:  # noqa: BLE001
            logger.warning("market 시세 %s 실패", name)
    return {"btc": None, "eth": None, "rs": None, "src": None}


def _fng():
    d = _get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
    return {"value": int(d["value"]), "label": d.get("value_classification", "")}


def _dominance():
    g = _get_json("https://api.coingecko.com/api/v3/global")["data"]
    mcp = g.get("market_cap_percentage", {}) or {}
    total = (g.get("total_market_cap", {}) or {}).get("usd") or 0
    btc_d = mcp.get("btc", 0) or 0
    eth_d = mcp.get("eth", 0) or 0
    others_d = max(0.0, 100 - sum(v for v in mcp.values() if isinstance(v, (int, float))))
    return {"btc": round(btc_d, 1), "eth": round(eth_d, 1), "others": round(others_d, 1),
            "mcap_chg": round(g.get("market_cap_change_percentage_24h_usd") or 0, 2),
            "total": total,
            "total2": total * (1 - btc_d / 100),               # BTC 제외 시총
            "total3": total * (1 - (btc_d + eth_d) / 100),       # BTC+ETH 제외 시총
            "others_usd": total * others_d / 100}                # 상위 제외(알트 잔여)


def _sectors():
    """크립토 섹터(카테고리) 24h 시총변화 — '지금 강한/약한 섹터'. CoinGecko categories."""
    cats = _get_json("https://api.coingecko.com/api/v3/coins/categories")
    rows = [c for c in cats if isinstance(c.get("market_cap_change_24h"), (int, float)) and (c.get("market_cap") or 0) > 0]
    rows.sort(key=lambda c: c["market_cap_change_24h"], reverse=True)

    def _row(c):
        return {"name": c.get("name") or "?", "chg24": round(c["market_cap_change_24h"], 2)}
    return {"top": [_row(c) for c in rows[:5]], "bottom": [_row(c) for c in rows[-3:]]}


def get_context(force=False):
    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["at"] < _TTL:
        return _CACHE["data"]
    md = _market_data()
    data = {"btc": md["btc"], "eth": md["eth"], "rs": md["rs"], "src": md["src"],
            "fng": None, "dom": None, "sectors": None, "ts": int(now)}
    for key, fn in (("fng", _fng), ("dom", _dominance), ("sectors", _sectors)):
        try:
            data[key] = fn()
        except Exception:  # noqa: BLE001
            logger.warning("market %s 실패", key)
    _CACHE["data"], _CACHE["at"] = data, now
    return data
