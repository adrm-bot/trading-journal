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


def _coins():
    """리전 차단 회피: Bybit(접근성 높음) → Binance 순으로 시도."""
    for name in ("bybit", "binance"):
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            btc = _coin(ex, "BTC/USDT")
            if btc:
                return btc, _coin(ex, "ETH/USDT"), name
        except Exception:  # noqa: BLE001
            logger.warning("market 시세 %s 실패", name)
    return None, None, None


def _fng():
    d = _get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
    return {"value": int(d["value"]), "label": d.get("value_classification", "")}


def _dominance():
    g = _get_json("https://api.coingecko.com/api/v3/global")["data"]
    return {"btc": round(g["market_cap_percentage"]["btc"], 1),
            "mcap_chg": round(g.get("market_cap_change_percentage_24h_usd") or 0, 2)}


def get_context(force=False):
    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["at"] < _TTL:
        return _CACHE["data"]
    btc, eth, src = _coins()
    data = {"btc": btc, "eth": eth, "src": src, "fng": None, "dom": None, "ts": int(now)}
    for key, fn in (("fng", _fng), ("dom", _dominance)):
        try:
            data[key] = fn()
        except Exception:  # noqa: BLE001
            logger.warning("market %s 실패", key)
    _CACHE["data"], _CACHE["at"] = data, now
    return data
