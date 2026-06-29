"""engine.py — 유저별 거래소 풀링 + 포지션 단위 재구성 + 행동분석. app 자립.

핵심 설계 (v2, 2026-06-29):
- 거래는 "닫는 주문 1건"이 아니라 **하나의 경제적 포지션(분할 진입/분할 청산 = open→flat 1사이클)** 단위로 적재한다.
- Bybit: closed-pnl을 시간·심볼·방향으로 그룹핑(강제청산 포함 완전, 손익 정확). 체결 워킹은 강제청산 체결을
  표준 execution 피드가 누락해 깨지므로 쓰지 않는다(실데이터로 검증됨).
- Binance: closed-pnl 등가가 없어 userTrades(체결, 강제청산 포함)를 부호 walk로 재구성, 실현손익은 거래소 보고값 합산.
- 거래소 예외는 키가 섞일 수 있어 `raise ... from None`으로 원문 체인을 끊는다.
"""
import logging
import os
import time
from datetime import datetime, timezone

import ccxt

from . import behaviors, db

logger = logging.getLogger("app.engine")

LOOKBACK = int(os.getenv("LOOKBACK_DAYS", "90"))
# 같은 심볼·방향에서 직전 청산으로부터 이 시간 내 청산이면 같은 포지션으로 묶는다.
POS_GAP_HOURS = float(os.getenv("POSITION_GAP_HOURS", "12"))


def _f(d, k):
    try:
        return float(d.get(k))
    except (TypeError, ValueError):
        return 0.0


def _ts_str(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _position_row(exchange, trade_id, symbol, direction, entry, exit_, qty, pnl,
                  opened_ms, closed_ms, fees=0.0, funding=0.0, leverage=0.0,
                  fill_count=0, liquidated=False):
    return {
        "trade_id": trade_id, "exchange": exchange, "symbol": symbol, "direction": direction,
        "entry": entry, "exit": exit_, "qty": qty, "pnl": pnl,
        "opened_at": _ts_str(opened_ms), "closed_at": _ts_str(closed_ms),
        "fees": round(fees, 6), "funding": round(funding, 6),
        "leverage": leverage or None, "fill_count": fill_count or None,
        "liquidated": 1 if liquidated else 0, "status": "의도 미기입",
    }


# ---------- Bybit: closed-pnl 그룹핑 ----------
def _bybit_closed_pnl(ex, lookback):
    now = ex.milliseconds()
    start = now - lookback * 86_400_000
    chunk = 7 * 86_400_000
    rows, w = [], start
    while w < now:
        e = min(w + chunk, now)
        cursor = None
        while True:
            p = {"category": "linear", "startTime": w, "endTime": e, "limit": 100}
            if cursor:
                p["cursor"] = cursor
            resp = ex.private_get_v5_position_closed_pnl(p)
            if str(resp.get("retCode")) != "0":
                raise RuntimeError("bybit closed-pnl rejected") from None
            res = resp.get("result", {})
            rows += res.get("list", [])
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
            time.sleep(0.15)
        w = e
    return rows


def reconstruct_bybit(rows):
    """closed-pnl 레코드들을 포지션(open→flat 사이클)으로 그룹핑."""
    for r in rows:
        r["_ct"] = int(r.get("createdTime") or r.get("updatedTime") or 0)
        r["_ut"] = int(r.get("updatedTime") or r.get("createdTime") or 0)
    rows.sort(key=lambda r: r["_ct"])
    gap = POS_GAP_HOURS * 3600 * 1000
    groups, cur = [], None
    for r in rows:
        # Bybit closed-pnl: side=Sell → 롱 청산, Buy → 숏 청산
        direction = "Long" if r.get("side") == "Sell" else "Short"
        sym = r.get("symbol", "")
        if cur and cur["symbol"] == sym and cur["dir"] == direction and r["_ct"] - cur["last"] <= gap:
            cur["rows"].append(r)
            cur["last"] = max(cur["last"], r["_ut"])
        else:
            if cur:
                groups.append(cur)
            cur = {"symbol": sym, "dir": direction, "rows": [r], "first": r["_ct"], "last": r["_ut"]}
    if cur:
        groups.append(cur)

    out = []
    for g in groups:
        rs = g["rows"]
        cs = sum(_f(r, "closedSize") for r in rs) or sum(_f(r, "qty") for r in rs)
        if cs <= 0:
            continue
        entry = sum(_f(r, "avgEntryPrice") * (_f(r, "closedSize") or _f(r, "qty")) for r in rs) / cs
        exit_ = sum(_f(r, "avgExitPrice") * (_f(r, "closedSize") or _f(r, "qty")) for r in rs) / cs
        pnl = sum(_f(r, "closedPnl") for r in rs)
        fees = sum(_f(r, "openFee") + _f(r, "closeFee") for r in rs)
        fill_count = sum(int(r.get("fillCount") or 1) for r in rs)
        leverage = max((_f(r, "leverage") for r in rs), default=0.0)
        liquidated = any(str(r.get("execType", "")).lower().startswith("bust")
                         or str(r.get("execType", "")).lower() == "adltrade" for r in rs)
        # 멱등키 = 가장 최근 청산주문 id (재풀링해도 안정: 마지막 청산이 윈도에서 가장 늦게 사라짐)
        last_row = max(rs, key=lambda r: r["_ut"])
        tid = f"bybit:pos:{g['symbol']}:{last_row.get('orderId', '')}"
        out.append(_position_row("bybit", tid, g["symbol"], g["dir"], round(entry, 10),
                                 round(exit_, 10), cs, round(pnl, 8), g["first"], g["last"],
                                 fees=fees, leverage=leverage, fill_count=fill_count, liquidated=liquidated))
    return out


def fetch_bybit(key, secret, lookback):
    ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True})
    try:
        rows = _bybit_closed_pnl(ex, lookback)
    except ccxt.BaseError:
        raise RuntimeError("bybit 인증/조회 실패 — read-only 키·권한을 확인하세요") from None
    return reconstruct_bybit(rows)


# ---------- Binance: userTrades 부호 walk ----------
def _binance_symbols_and_funding(ex, lookback):
    now = ex.milliseconds()
    start = now - lookback * 86_400_000
    week = 7 * 86_400_000
    symbols, funding = set(), {}
    cur = start
    while cur < now:
        e = min(cur + week, now)
        for inc in ex.fapiPrivateGetIncome({"startTime": cur, "endTime": e, "limit": 1000}):
            typ = inc.get("incomeType")
            sym = inc.get("symbol") or ""
            if typ == "REALIZED_PNL" and sym:
                symbols.add(sym)
            elif typ == "FUNDING_FEE" and sym:
                funding[sym] = funding.get(sym, 0.0) + _f(inc, "income")
        cur = e
    return symbols, funding


def _binance_user_trades(ex, symbol, lookback):
    now = ex.milliseconds()
    start = now - lookback * 86_400_000
    week = 7 * 86_400_000
    out, cur = [], start
    while cur < now:
        e = min(cur + week, now)
        from_id = None
        while True:
            p = {"symbol": symbol, "startTime": cur, "endTime": e, "limit": 1000}
            if from_id is not None:
                p = {"symbol": symbol, "fromId": from_id, "limit": 1000}
            batch = ex.fapiPrivateGetUserTrades(p)
            if not batch:
                break
            out += batch
            if len(batch) < 1000:
                break
            from_id = int(batch[-1]["id"]) + 1  # 완전 페이지네이션
        cur = e
    # 중복 제거(시간창+fromId 혼용 안전장치)
    seen, uniq = set(), []
    for t in out:
        if t["id"] not in seen:
            seen.add(t["id"])
            uniq.append(t)
    return uniq


def _new_bpos(direction, ts, anchor):
    return {"dir": direction, "en": 0.0, "eq": 0.0, "xn": 0.0, "xq": 0.0,
            "fee": 0.0, "rp": 0.0, "n": 0, "o": ts, "c": ts, "anchor": anchor, "liq": False}


def reconstruct_walk(exchange, symbol, trades, funding_total=0.0):
    """체결을 positionSide별 부호 walk → open→flat 포지션. 진행중 제외. (Binance/Gate 공용)"""
    buckets = {}
    for t in trades:
        buckets.setdefault(t.get("positionSide") or "BOTH", []).append(t)
    out = []
    for fills in buckets.values():
        fills.sort(key=lambda t: (int(t["time"]), int(t["id"])))
        peak = max((abs(float(t["qty"])) for t in fills), default=1.0)
        tol = max(peak * 1e-6, 1e-12)
        pos, net = None, 0.0
        for t in fills:
            q = abs(float(t["qty"]))
            if q <= 0:
                continue
            s = 1 if str(t.get("side")).upper() == "BUY" else -1
            price = float(t["price"])
            fee = _f(t, "commission")
            realized = _f(t, "realizedPnl")
            ts = int(t["time"])
            if pos is None:
                pos = _new_bpos("Long" if s > 0 else "Short", ts, str(t["id"]))
            inc = (s > 0) == (pos["dir"] == "Long")  # 포지션과 같은 방향 = 증가
            pos["n"] += 1
            pos["c"] = ts
            if inc:  # 진입/물타기
                pos["en"] += price * q; pos["eq"] += q; pos["fee"] += fee; pos["rp"] += realized
                net += s * q
            else:    # 감소/청산/플립
                close_q = min(q, abs(net))
                frac = close_q / q if q else 1.0
                pos["xn"] += price * close_q; pos["xq"] += close_q
                pos["fee"] += fee * frac; pos["rp"] += realized
                net += (-close_q if net > 0 else close_q)  # net을 0쪽으로
                leftover = q - close_q
                if abs(net) <= tol:  # flat → emit
                    out.append(_finalize_pos(exchange, symbol, pos, str(t["id"])))
                    pos = None
                    if leftover > tol:  # 플립: 잔여로 반대 포지션 오픈
                        nd = "Long" if s > 0 else "Short"
                        pos = _new_bpos(nd, ts, str(t["id"]))
                        pos["en"] = price * leftover; pos["eq"] = leftover
                        pos["fee"] = fee * (leftover / q); pos["n"] = 1
                        net = (1 if nd == "Long" else -1) * leftover
        # 루프 종료 후 pos가 남으면 진행중 → 제외
    if out and funding_total:
        out[-1]["funding"] = round(funding_total, 6)  # 펀딩 심볼합을 최근 포지션에 귀속(근사)
    return out


def _finalize_pos(exchange, symbol, pos, closing_id):
    eq, xq = pos["eq"], pos["xq"]
    entry = pos["en"] / eq if eq else 0.0
    exit_ = pos["xn"] / xq if xq else 0.0
    dsign = 1 if pos["dir"] == "Long" else -1
    # 실현손익: 거래소 보고 합(rp) 우선, 없으면 vwap 계산으로 폴백(Gate 등 fill에 pnl 미포함 대비)
    pnl = pos["rp"] if abs(pos["rp"]) > 1e-9 else dsign * (exit_ - entry) * xq - pos["fee"]
    tid = f"{exchange}:pos:{symbol}:{closing_id}"
    return _position_row(exchange, tid, symbol, pos["dir"], round(entry, 10), round(exit_, 10),
                         round(eq, 10), round(pnl, 8), pos["o"], pos["c"],
                         fees=pos["fee"], fill_count=pos["n"], liquidated=pos["liq"])


def fetch_binance(key, secret, lookback):
    ex = ccxt.binanceusdm({"apiKey": key, "secret": secret, "enableRateLimit": True})
    try:
        symbols, funding = _binance_symbols_and_funding(ex, lookback)
        rows = []
        for sym in sorted(symbols):
            trades = _binance_user_trades(ex, sym, lookback)
            rows += reconstruct_walk("binance", sym, trades, funding.get(sym, 0.0))
        return rows
    except ccxt.BaseError:
        raise RuntimeError("binance 인증/조회 실패 — USDⓈ-M read-only 키·권한·시간동기를 확인하세요") from None


# ---------- Gate.io: ccxt 통합 체결 → walk (로컬 키 없음, 방어적·라이브 검증 필요) ----------
def _ccxt_to_fill(t):
    """ccxt 통합 trade dict → walk용 정규화 fill."""
    info = t.get("info") or {}
    realized = info.get("pnl") or info.get("realised_pnl") or info.get("realizedPnl") or 0
    return {"time": int(t.get("timestamp") or 0), "id": str(t.get("id") or t.get("order") or ""),
            "side": str(t.get("side") or "").upper(), "price": float(t.get("price") or 0),
            "qty": abs(float(t.get("amount") or 0)), "commission": float((t.get("fee") or {}).get("cost") or 0),
            "realizedPnl": float(realized or 0), "positionSide": "BOTH"}


def fetch_gate(key, secret, lookback):
    gate_cls = getattr(ccxt, "gate", None) or getattr(ccxt, "gateio")
    ex = gate_cls({"apiKey": key, "secret": secret, "enableRateLimit": True,
                   "options": {"defaultType": "swap", "defaultSettle": "usdt"}})
    now = ex.milliseconds()
    start = now - lookback * 86_400_000
    try:
        ex.load_markets()
        raw = ex.fetch_my_trades(None, start, None, {"type": "swap", "settle": "usdt"})
        bysym = {}
        for t in raw:
            sym = t.get("symbol") or "?"
            bysym.setdefault(sym, []).append(_ccxt_to_fill(t))
        rows = []
        for sym, fills in bysym.items():
            rows += reconstruct_walk("gate", sym.replace("/", "").split(":")[0], fills)
        return rows
    except ccxt.BaseError:
        raise RuntimeError("gate 인증/조회 실패 — USDT 무기한 read-only 키·권한을 확인하세요") from None


# ---------- 키 저장 전 read-only 권한 프로빙 (거래/출금 권한 있으면 거부) ----------
PROBE_TIMEOUT = int(os.getenv("PROBE_TIMEOUT_MS", "10000"))


def _probe_bybit(key, secret):
    ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True, "timeout": PROBE_TIMEOUT})
    try:
        resp = ex.private_get_v5_user_query_api({})
    except ccxt.BaseError:
        raise RuntimeError("bybit 키 검증 실패 — 키/시크릿/IP 화이트리스트를 확인하세요") from None
    if str(resp.get("retCode")) != "0":
        raise RuntimeError("bybit 키 검증 실패 — 권한 조회가 거부되었습니다") from None
    res = resp.get("result", {}) or {}
    perms = res.get("permissions", {}) or {}
    trade_groups = ["ContractTrade", "Spot", "Derivatives", "Options", "CopyTrading", "Exchange", "NFT"]
    has_trade = any(perms.get(g) for g in trade_groups)
    wallet = [str(x).lower() for x in (perms.get("Wallet") or [])]
    has_withdraw = any("withdraw" in w for w in wallet)
    read_only_flag = str(res.get("readOnly")) in ("1", "True", "true")
    if has_withdraw:
        raise RuntimeError("이 키에는 출금 권한이 있습니다. 출금 권한이 없는 read-only 키를 발급해 다시 시도하세요.")
    if has_trade or (res.get("readOnly") is not None and not read_only_flag):
        raise RuntimeError("이 키에는 거래/주문 권한이 있습니다. '읽기 전용(read-only)' 키만 등록할 수 있습니다.")
    return {"ok": True, "warn": None}


def _probe_binance(key, secret):
    ex = ccxt.binanceusdm({"apiKey": key, "secret": secret, "enableRateLimit": True, "timeout": PROBE_TIMEOUT})
    try:
        r = ex.sapiGetAccountApiRestrictions()
    except ccxt.BaseError:
        raise RuntimeError("binance 키 검증 실패 — USDⓈ-M(또는 동일 마스터) read-only 키·IP·시간동기를 확인하세요") from None

    def _t(v):
        return v is True or str(v).lower() == "true"
    if _t(r.get("enableWithdrawals")):
        raise RuntimeError("이 키에는 출금 권한이 있습니다. 출금 권한이 없는 read-only 키를 발급해 다시 시도하세요.")
    if _t(r.get("enableSpotAndMarginTrading")) or _t(r.get("enableFutures")) or _t(r.get("enableMargin")) or _t(r.get("enableInternalTransfer")):
        raise RuntimeError("이 키에는 거래/선물/이체 권한이 있습니다. '읽기 전용(read-only)' 키만 등록할 수 있습니다.")
    return {"ok": True, "warn": None}


def _probe_gate(key, secret):
    gate_cls = getattr(ccxt, "gate", None) or getattr(ccxt, "gateio")
    ex = gate_cls({"apiKey": key, "secret": secret, "enableRateLimit": True, "timeout": PROBE_TIMEOUT,
                   "options": {"defaultType": "swap", "defaultSettle": "usdt"}})
    try:
        ex.fetch_balance({"type": "swap", "settle": "usdt"})
    except ccxt.BaseError:
        raise RuntimeError("gate 키 검증 실패 — USDT 무기한 read-only 키·권한·IP를 확인하세요") from None
    return {"ok": True, "warn": "Gate는 권한 자동검증이 제한적입니다 — 반드시 '읽기 전용' 키를 사용하세요."}


_PROBES = {"bybit": _probe_bybit, "binance": _probe_binance, "gate": _probe_gate}


def probe_readonly(kind, key, secret):
    """키 저장 전 read-only 강제. 거래/출금 권한 있으면 RuntimeError(사용자 메시지). 반환 {ok,warn}."""
    fn = _PROBES.get(kind)
    if not fn:
        return {"ok": True, "warn": None}
    return fn(key, secret)


ADAPTERS = {"bybit": fetch_bybit, "binance": fetch_binance, "gate": fetch_gate}


def pull_user(uid):
    """거래소별 적재 결과를 반환: {exchange: {"added": n, "error": str|None}}."""
    results = {}
    for kind in db.list_connections(uid):
        if kind not in ADAPTERS:
            continue
        cred = db.get_connection(uid, kind)
        try:
            added = sum(db.upsert_trade(uid, r) for r in ADAPTERS[kind](cred["key"], cred["secret"], LOOKBACK))
            results[kind] = {"added": added, "error": None}
        except RuntimeError as e:
            logger.warning("pull %s 실패 uid=%s: %s", kind, uid, e)
            results[kind] = {"added": 0, "error": str(e)}
        except Exception:  # noqa: BLE001
            logger.exception("pull %s 예외 uid=%s", kind, uid)
            results[kind] = {"added": 0, "error": f"{kind} 적재 중 알 수 없는 오류"}
    return results


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
