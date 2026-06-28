"""engine.py — 유저별 거래소 풀링(ccxt, 키는 인자로만) + 행동분석. app 자립(journal/ 의존 없음).

거래소 예외는 키가 섞일 수 있으므로 `raise ... from None`으로 원문 체인을 끊는다.
"""
import os
import time
from datetime import datetime, timezone

import ccxt

from . import behaviors, db

LOOKBACK = int(os.getenv("LOOKBACK_DAYS", "30"))


def _f(d, k):
    try:
        return float(d.get(k))
    except (TypeError, ValueError):
        return None


def _row(exchange, trade_id, updated_ms, symbol, direction, entry, exit_, qty, pnl):
    dt = datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    return {"trade_id": trade_id, "exchange": exchange, "symbol": symbol, "direction": direction,
            "entry": entry, "exit": exit_, "qty": qty, "pnl": pnl,
            "closed_at": dt.strftime("%Y-%m-%d %H:%M:%S"), "status": "의도 미기입"}


def fetch_bybit(key, secret, lookback):
    ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True})
    now = ex.milliseconds()
    start = now - lookback * 86_400_000
    chunk = 7 * 86_400_000
    rows, w = [], start
    try:
        while w < now:
            e = min(w + chunk, now)
            cursor = None
            while True:
                p = {"category": "linear", "startTime": w, "endTime": e, "limit": 100}
                if cursor:
                    p["cursor"] = cursor
                resp = ex.private_get_v5_position_closed_pnl(p)
                if str(resp.get("retCode")) != "0":
                    raise RuntimeError("bybit fetch rejected") from None
                res = resp.get("result", {})
                for r in res.get("list", []):
                    sym, oid = r.get("symbol", ""), r.get("orderId", "")
                    upd = int(r.get("updatedTime") or r.get("createdTime") or 0)
                    s = (r.get("side") or "").lower()
                    direction = "Long" if s == "sell" else ("Short" if s == "buy" else None)
                    rows.append(_row("bybit", f"bybit:{sym}:{oid}:{upd}", upd, sym, direction,
                                     _f(r, "avgEntryPrice"), _f(r, "avgExitPrice"),
                                     _f(r, "qty") or _f(r, "closedSize"), _f(r, "closedPnl")))
                cursor = res.get("nextPageCursor")
                if not cursor:
                    break
                time.sleep(0.2)
            w = e
    except ccxt.BaseError:
        raise RuntimeError("bybit auth/fetch failed") from None
    return rows


def fetch_binance(key, secret, lookback):
    ex = ccxt.binanceusdm({"apiKey": key, "secret": secret, "enableRateLimit": True})
    now = ex.milliseconds()
    cur, week = now - lookback * 86_400_000, 7 * 86_400_000
    rows = []
    try:
        while cur < now:
            e = min(cur + week, now)
            for inc in ex.fapiPrivateGetIncome({"incomeType": "REALIZED_PNL", "startTime": cur, "endTime": e, "limit": 1000}):
                pnl = _f(inc, "income")
                if pnl is None:
                    continue
                sym, t, tran = inc.get("symbol", ""), int(inc.get("time", 0)), inc.get("tranId", "")
                rows.append(_row("binance", f"binance:{sym}:{tran}:{t}", t, sym, None, None, None, None, pnl))
            cur = e
    except ccxt.BaseError:
        raise RuntimeError("binance auth/fetch failed") from None
    return rows


ADAPTERS = {"bybit": fetch_bybit, "binance": fetch_binance}


def pull_user(uid) -> int:
    """신규 적재 건수만 반환(멱등)."""
    added = 0
    for kind in db.list_connections(uid):
        if kind in ADAPTERS:
            cred = db.get_connection(uid, kind)
            for r in ADAPTERS[kind](cred["key"], cred["secret"], LOOKBACK):
                added += db.upsert_trade(uid, r)
    return added


def _parse(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def analyze_user(uid):
    trades = db.get_trades(uid)
    rows = [{"실현손익(USDT)": t["pnl"], "상태": t["status"], "심볼": t["symbol"],
             "방향": t["direction"], "청산시각": _parse(t["closed_at"]), "trade_id": t["trade_id"]} for t in trades]
    summary = behaviors.analyze(rows)
    summary = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in summary.items()}
    return summary, trades
