"""engine.py — 유저별 거래소 풀링 + 포지션 단위 재구성 + 행동분석. app 자립.

핵심 설계 (v2, 2026-06-29):
- 거래는 "닫는 주문 1건"이 아니라 **하나의 경제적 포지션(분할 진입/분할 청산 = open→flat 1사이클)** 단위로 적재한다.
- Bybit: closed-pnl을 시간·심볼·방향으로 그룹핑(강제청산 포함 완전, 손익 정확). 체결 워킹은 강제청산 체결을
  표준 execution 피드가 누락해 깨지므로 쓰지 않는다(실데이터로 검증됨).
- Binance: closed-pnl 등가가 없어 userTrades(체결, 강제청산 포함)를 부호 walk로 재구성, 실현손익은 거래소 보고값 합산.
- 거래소 예외는 키가 섞일 수 있어 `raise ... from None`으로 원문 체인을 끊는다.
"""
import json
import logging
import os
import time
import urllib.request
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
                  fill_count=0, liquidated=False, exit_reason="unknown",
                  exit_count=0, exit_legs=None):
    return {
        "trade_id": trade_id, "exchange": exchange, "symbol": symbol, "direction": direction,
        "entry": entry, "exit": exit_, "qty": qty, "pnl": pnl,
        "opened_at": _ts_str(opened_ms), "closed_at": _ts_str(closed_ms),
        "fees": round(fees, 6), "funding": round(funding, 6),
        "leverage": leverage or None, "fill_count": fill_count or None,
        "liquidated": 1 if liquidated else 0, "exit_reason": exit_reason, "status": "의도 미기입",
        "exit_count": exit_count or None, "exit_legs": exit_legs,  # 분할청산 레그(JSON)
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


def _bybit_executions(ex, lookback):
    """청산기준 라벨용 보강 소스: orderId → stopOrderType 맵. 권한/조회 실패 시 빈 맵(graceful degrade)."""
    now = ex.milliseconds()
    start = now - lookback * 86_400_000
    chunk = 7 * 86_400_000
    out = {}
    try:
        w = start
        while w < now:
            e = min(w + chunk, now)
            cursor = None
            while True:
                p = {"category": "linear", "startTime": w, "endTime": e, "limit": 100}
                if cursor:
                    p["cursor"] = cursor
                res = ex.private_get_v5_execution_list(p).get("result", {})
                for f in res.get("list", []):
                    oid, st = f.get("orderId"), (f.get("stopOrderType") or "")
                    if not oid:
                        continue
                    if st:
                        out[oid] = st          # stopOrderType 있으면 채택(우선)
                    else:
                        out.setdefault(oid, "")  # 확인된 주문(수동)
                cursor = res.get("nextPageCursor")
                if not cursor:
                    break
                time.sleep(0.12)
            w = e
    except ccxt.BaseError:
        return {}
    return out


def _exit_reason(liquidated, oid, exec_map):
    """청산기준 라벨: liquidation > sl_hit/tp_hit/trailing(stopOrderType) > manual(주문확인됨) > unknown."""
    if liquidated:
        return "liquidation"
    if not exec_map:
        return "unknown"
    st = str(exec_map.get(oid, None) if oid in exec_map else "__none__").lower()
    if st == "__none__":
        return "unknown"
    if "trailing" in st:
        return "trailing"
    if "stoploss" in st or st == "stop":
        return "sl_hit"
    if "takeprofit" in st or "profit" in st:
        return "tp_hit"
    return "manual"


def reconstruct_bybit(rows, exec_map=None):
    """closed-pnl 레코드들을 포지션(open→flat 사이클)으로 그룹핑. exec_map={orderId:stopOrderType}로 청산기준 라벨."""
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
        oid = last_row.get("orderId", "")
        tid = f"bybit:pos:{g['symbol']}:{oid}"
        out.append(_position_row("bybit", tid, g["symbol"], g["dir"], round(entry, 10),
                                 round(exit_, 10), cs, round(pnl, 8), g["first"], g["last"],
                                 fees=fees, leverage=leverage, fill_count=fill_count, liquidated=liquidated,
                                 exit_reason=_exit_reason(liquidated, oid, exec_map)))
    return out


def fetch_bybit(key, secret, lookback):
    ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True})
    try:
        rows = _bybit_closed_pnl(ex, lookback)
    except ccxt.BaseError:
        raise RuntimeError("bybit 인증/조회 실패. read-only 키·권한을 확인하세요") from None
    exec_map = _bybit_executions(ex, lookback)  # 청산기준 보강(실패해도 graceful)
    return reconstruct_bybit(rows, exec_map)


# ---------- Binance: userTrades 부호 walk ----------
def _binance_symbols_and_funding(ex, lookback):
    now = ex.milliseconds()
    start = now - lookback * 86_400_000
    week = 7 * 86_400_000
    symbols, fevents = set(), {}
    cur = start
    while cur < now:
        e = min(cur + week, now)
        ws = cur
        while True:  # 윈도 내 페이지네이션(1000 초과 시 최근 거래 누락 방지)
            batch = ex.fapiPrivateGetIncome({"startTime": ws, "endTime": e, "limit": 1000})
            if not batch:
                break
            for inc in batch:
                typ = inc.get("incomeType")
                sym = inc.get("symbol") or ""
                # 심볼 발견: 거래가 있었으면 REALIZED_PNL이든 COMMISSION이든 잡는다(누락 방지)
                if sym and typ in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                    symbols.add(sym)
                if typ == "FUNDING_FEE" and sym:  # 시각 보존 → 포지션별 귀속용
                    fevents.setdefault(sym, []).append((int(inc.get("time") or 0), _f(inc, "income")))
            if len(batch) < 1000:
                break
            ws = int(batch[-1].get("time") or ws) + 1
        cur = e
    return symbols, fevents


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
            "fee": 0.0, "rp": 0.0, "n": 0, "o": ts, "c": ts, "anchor": anchor, "liq": False,
            "legs": []}  # 청산 레그(가격, 수량) — 분할청산 충실도/레그 R용


def reconstruct_walk(exchange, symbol, trades, funding_events=None):
    """체결을 positionSide별 부호 walk → open→flat 포지션. 진행중 제외. (Binance/Gate 공용)
    funding_events: [(ts_ms, amount), ...] — 각 펀딩을 그 시각을 포함하는 포지션에 귀속(공백이면 마지막)."""
    buckets = {}
    for t in trades:
        buckets.setdefault(t.get("positionSide") or "BOTH", []).append(t)
    out, windows = [], []  # windows[i] = (open_ms, close_ms) for out[i] — 펀딩 시각귀속용
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
                # 청산 레그: 같은 청산 주문의 분할 체결(거래소가 한 주문을 수십 체결로 쪼갬)은 한 레그로
                # 병합(vwap). orderId 없으면(주문 정보 미제공) 체결별 레그로 폴백. P&L·vwap엔 영향 없음.
                oid = str(t.get("orderId") or t.get("order") or "")
                if oid and pos["legs"] and pos.get("_loid") == oid:
                    pl, pq = pos["legs"][-1]; nq = pq + close_q
                    pos["legs"][-1] = (round((pl * pq + price * close_q) / nq, 10), round(nq, 10))
                else:
                    pos["legs"].append((round(price, 10), round(close_q, 10)))
                pos["_loid"] = oid
                pos["fee"] += fee * frac; pos["rp"] += realized
                net += (-close_q if net > 0 else close_q)  # net을 0쪽으로
                leftover = q - close_q
                if abs(net) <= tol:  # flat → emit
                    out.append(_finalize_pos(exchange, symbol, pos, str(t["id"])))
                    windows.append((pos["o"], pos["c"]))
                    pos = None
                    if leftover > tol:  # 플립: 잔여로 반대 포지션 오픈
                        nd = "Long" if s > 0 else "Short"
                        pos = _new_bpos(nd, ts, str(t["id"]))
                        pos["en"] = price * leftover; pos["eq"] = leftover
                        pos["fee"] = fee * (leftover / q); pos["n"] = 1
                        net = (1 if nd == "Long" else -1) * leftover
        # 루프 종료 후 pos가 남으면 진행중 → 제외
    # 펀딩 귀속: 각 이벤트를 그 시각을 포함하는 포지션에, 어디에도 안 들면 마지막 포지션에
    if out and funding_events:
        for fts, amt in funding_events:
            idx = next((i for i, (o, c) in enumerate(windows) if o <= fts <= c), len(out) - 1)
            out[idx]["funding"] = round((out[idx].get("funding") or 0.0) + amt, 8)
    return out


def _finalize_pos(exchange, symbol, pos, closing_id):
    eq, xq = pos["eq"], pos["xq"]
    entry = pos["en"] / eq if eq else 0.0
    exit_ = pos["xn"] / xq if xq else 0.0
    dsign = 1 if pos["dir"] == "Long" else -1
    # 실현손익: 거래소 보고 합(rp) 우선, 없으면 vwap 계산으로 폴백(Gate 등 fill에 pnl 미포함 대비)
    pnl = pos["rp"] if abs(pos["rp"]) > 1e-9 else dsign * (exit_ - entry) * xq - pos["fee"]
    tid = f"{exchange}:pos:{symbol}:{closing_id}"
    legs = pos.get("legs") or []
    return _position_row(exchange, tid, symbol, pos["dir"], round(entry, 10), round(exit_, 10),
                         round(eq, 10), round(pnl, 8), pos["o"], pos["c"],
                         fees=pos["fee"], fill_count=pos["n"], liquidated=pos["liq"],
                         exit_count=len(legs), exit_legs=(json.dumps(legs) if len(legs) > 1 else None))


def _mae_mfe_prices(ohlcv, direction, o_ms, c_ms):
    """보유구간[o_ms,c_ms]에 걸친 캔들(ccxt OHLCV [ts,o,h,l,c,v])의 최저/최고가 →
    (mae_price, mfe_price). 롱: mae=최저(가장 불리)·mfe=최고(가장 유리), 숏: 반대.
    구간 캔들 없으면 (None,None). 순수함수 — 네트워크 비의존(단위테스트 가능)."""
    lows, highs = [], []
    for c in ohlcv or []:
        if not c or len(c) < 5 or c[0] is None:
            continue
        if c[0] < o_ms or c[0] > c_ms:
            continue
        if c[3] is not None:
            lows.append(c[3])   # low
        if c[2] is not None:
            highs.append(c[2])  # high
    if not lows or not highs:
        return None, None
    lo, hi = min(lows), max(highs)
    return (hi, lo) if direction == "Short" else (lo, hi)


def fetch_binance(key, secret, lookback):
    ex = ccxt.binanceusdm({"apiKey": key, "secret": secret, "enableRateLimit": True})
    try:
        symbols, fevents = _binance_symbols_and_funding(ex, lookback)
        rows = []
        for sym in sorted(symbols):
            trades = _binance_user_trades(ex, sym, lookback)
            rows += reconstruct_walk("binance", sym, trades, fevents.get(sym))
        return rows
    except ccxt.BaseError:
        raise RuntimeError("binance 인증/조회 실패. USDⓈ-M read-only 키·권한·시간동기를 확인하세요") from None


# ---------- Gate.io: ccxt 통합 체결 → walk (로컬 키 없음, 방어적·라이브 검증 필요) ----------
def _ccxt_to_fill(t):
    """ccxt 통합 trade dict → walk용 정규화 fill."""
    info = t.get("info") or {}
    realized = info.get("pnl") or info.get("realised_pnl") or info.get("realizedPnl") or 0
    return {"time": int(t.get("timestamp") or 0), "id": str(t.get("id") or t.get("order") or ""),
            "order": str(t.get("order") or info.get("order_id") or info.get("orderId") or ""),  # 레그 병합용
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
        raise RuntimeError("gate 인증/조회 실패. USDT 무기한 read-only 키·권한을 확인하세요") from None


# ---------- 키 저장 전 read-only 권한 프로빙 (거래/출금 권한 있으면 거부) ----------
PROBE_TIMEOUT = int(os.getenv("PROBE_TIMEOUT_MS", "10000"))


def _probe_bybit(key, secret):
    ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True, "timeout": PROBE_TIMEOUT})
    try:
        resp = ex.private_get_v5_user_query_api({})
    except ccxt.BaseError:
        raise RuntimeError("bybit 키 검증 실패. 키·시크릿·IP 화이트리스트를 확인하세요") from None
    if str(resp.get("retCode")) != "0":
        raise RuntimeError("bybit 키 검증 실패. 권한 조회가 거부되었습니다") from None
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
        raise RuntimeError("binance 키 검증 실패. USDⓈ-M(또는 동일 마스터) read-only 키·IP·시간동기를 확인하세요") from None

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
        raise RuntimeError("gate 키 검증 실패. USDT 무기한 read-only 키·권한·IP를 확인하세요") from None
    return {"ok": True, "warn": "Gate는 권한 자동검증이 제한적입니다. 반드시 '읽기 전용' 키를 사용하세요."}


_PROBES = {"bybit": _probe_bybit, "binance": _probe_binance, "gate": _probe_gate}


def probe_readonly(kind, key, secret):
    """키 저장 전 read-only 강제. 거래/출금 권한 있으면 RuntimeError(사용자 메시지). 반환 {ok,warn}."""
    fn = _PROBES.get(kind)
    if not fn:
        return {"ok": True, "warn": None}
    return fn(key, secret)


# ---------- NinjaTrader — 공식 API(Tradovate 백엔드) REST ----------
# 크립토 거래소와 달리 read-only 키 개념이 없다(계정 인증 + 앱 키). 저장은 봉투암호화,
# 등록 시 사용자 명시 확인(ack) 필수. 손익은 USD 기준(표시는 USDT 단위와 동일 취급).
NT_BASES = ("https://live.tradovateapi.com/v1", "https://demo.tradovateapi.com/v1")
NT_APP_ID, NT_APP_VER = "mmj-journal", "1.0"
NT_TIMEOUT = int(os.getenv("NT_TIMEOUT_SEC", "20"))


def _nt_http(base, path, token=None, payload=None):
    """Tradovate REST 1회 호출(stdlib) — payload 있으면 POST(JSON), 없으면 GET."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=NT_TIMEOUT) as r:
        return json.loads(r.read().decode())


def _nt_auth(cred):
    """저장된 env 우선, 그다음 live→demo 순으로 인증. 성공 시 (base, token).
    MFA/캡차 요구(p-ticket)는 진행 불가 — 명시 에러로 안내."""
    try:
        cid = int(str(cred.get("cid")).strip())
    except (TypeError, ValueError):
        raise RuntimeError("ninjatrader API CID는 숫자여야 합니다") from None
    body = {"name": cred.get("name"), "password": cred.get("password"),
            "appId": NT_APP_ID, "appVersion": NT_APP_VER,
            "cid": cid, "sec": cred.get("sec"),
            "deviceId": ("mmj-" + str(cred.get("name") or ""))[:64]}
    order = NT_BASES if cred.get("env") != "demo" else (NT_BASES[1], NT_BASES[0])
    errs = []
    for base in order:
        env = "demo" if "demo" in base else "live"
        try:
            j = _nt_http(base, "/auth/accesstokenrequest", payload=body)
        except Exception:  # noqa: BLE001 — 4xx 포함
            errs.append(f"{env} 인증 거부")
            continue
        if j.get("accessToken"):
            return base, j["accessToken"]
        if j.get("p-ticket"):
            raise RuntimeError("ninjatrader 인증에 추가 확인(MFA/캡차)이 걸렸습니다. "
                               "Tradovate/NinjaTrader 웹에서 전용 API 키(cid/sec)를 발급해 등록해 주세요")
        errs.append(f"{env}: {j.get('errorText') or '인증 거부'}")
    raise RuntimeError("ninjatrader 인증 실패 · " + " / ".join(errs))


def _nt_items(base, tok, entity, ids):
    """/{entity}/items?ids=… 배치 조회(50개씩). 실패 청크는 건너뜀(부분 성공 허용)."""
    uniq = sorted({int(i) for i in ids if i is not None})
    out = []
    for i in range(0, len(uniq), 50):
        chunk = ",".join(map(str, uniq[i:i + 50]))
        try:
            out += _nt_http(base, f"/{entity}/items?ids={chunk}", token=tok) or []
        except Exception:  # noqa: BLE001
            logger.warning("ninjatrader %s 배치 조회 실패", entity)
    return out


def _nt_contract_map(base, tok, contract_ids):
    """contractId → (심볼명, 포인트가치). contract→maturity→product 체인.
    해석 실패 계약은 제외 — 잘못된 배수로 손익을 오염시키지 않는다."""
    contracts = _nt_items(base, tok, "contract", contract_ids)
    mats = {m["id"]: m for m in _nt_items(base, tok, "contractMaturity",
                                          [c.get("contractMaturityId") for c in contracts]) if m.get("id") is not None}
    prods = {p["id"]: p for p in _nt_items(base, tok, "product",
                                           [m.get("productId") for m in mats.values()]) if p.get("id") is not None}
    out = {}
    for c in contracts:
        m = mats.get(c.get("contractMaturityId")) or {}
        p = prods.get(m.get("productId")) or {}
        vpp = p.get("valuePerPoint")
        if c.get("id") is None or not c.get("name") or not vpp:
            continue
        out[c["id"]] = (str(c["name"]), float(vpp))
    return out


def _nt_ts_ms(s):
    """Tradovate ISO 타임스탬프('…Z') → unix ms. 파싱 불가면 None. (순수함수)"""
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _nt_fill_fee_map(base, tok, fill_ids):
    """fillId → 수수료 합(커미션+거래소·청산·NFA·라우팅). 조회 실패 시 빈 dict(수수료 0 처리)."""
    fees = {}
    for f in _nt_items(base, tok, "fillFee", fill_ids):
        if f.get("id") is None:
            continue
        fees[f["id"]] = sum(float(f.get(k) or 0) for k in
                            ("commission", "clearingFee", "exchangeFee", "nfaFee", "brokerageFee", "orderRoutingFee"))
    return fees


def _nt_normalize_fills(fills, fees_by_id, start_ms):
    """Tradovate fill → reconstruct_walk용 정규화, contractId별 그룹. (순수함수 — 단위테스트 가능)
    lookback 이전·필드 불량 체결은 제외. realizedPnl은 0 — 손익은 _nt_scale_pnl에서 포인트가치로 계산."""
    by_contract = {}
    for f in fills or []:
        ts = _nt_ts_ms(f.get("timestamp"))
        if ts is None or ts < start_ms:
            continue
        act = str(f.get("action") or "").upper()
        if act not in ("BUY", "SELL") or f.get("price") is None or not f.get("qty"):
            continue
        by_contract.setdefault(f.get("contractId"), []).append({
            "id": f.get("id"), "time": ts, "side": act, "price": float(f["price"]),
            "qty": abs(float(f["qty"])), "commission": float(fees_by_id.get(f.get("id")) or 0.0),
            "realizedPnl": 0.0, "positionSide": "BOTH"})
    by_contract.pop(None, None)
    return by_contract


def _nt_scale_pnl(rows, vpp):
    """선물 포인트가치(vpp) 반영해 pnl 재계산 — walk의 가격차 폴백은 계약 배수를 모른다.
    point_value도 행에 저장 → enrich의 리스크·습관비용 달러 환산에 쓰임. (순수함수)"""
    for r in rows:
        dsign = 1 if r["direction"] == "Long" else -1
        gross = dsign * (r["exit"] - r["entry"]) * r["qty"] * vpp
        r["pnl"] = round(gross - (r.get("fees") or 0.0), 8)
        r["point_value"] = vpp
    return rows


def fetch_ninjatrader(cred, lookback):
    base, tok = _nt_auth(cred)
    start_ms = int(time.time() * 1000) - lookback * 86_400_000
    try:
        fills = _nt_http(base, "/fill/list", token=tok) or []
    except Exception:  # noqa: BLE001
        raise RuntimeError("ninjatrader 체결 조회 실패. API 키(cid/sec) 권한과 계정 상태를 확인하세요") from None
    recent_ids = [f.get("id") for f in fills if (_nt_ts_ms(f.get("timestamp")) or 0) >= start_ms]
    fees = _nt_fill_fee_map(base, tok, recent_ids)
    by_contract = _nt_normalize_fills(fills, fees, start_ms)
    cmap = _nt_contract_map(base, tok, by_contract.keys())
    rows = []
    for cid, cf in by_contract.items():
        info = cmap.get(cid)
        if not info:
            logger.warning("ninjatrader 계약 %s 해석 실패 — 해당 체결 건너뜀(손익 오염 방지)", cid)
            continue
        sym, vpp = info
        rows += _nt_scale_pnl(reconstruct_walk("ninjatrader", sym, cf), vpp)
    return rows


def probe_ninjatrader(cred):
    """등록 전 인증 확인 — read-only 제한이 불가한 API라 권한 프로빙 대신 인증 성공 여부와
    환경(live/demo)만 확인해 반환."""
    base, _ = _nt_auth(cred)
    return "demo" if "demo" in base else "live"


def _nt_open_positions(cred):
    base, tok = _nt_auth(cred)
    poss = _nt_http(base, "/position/list", token=tok) or []
    live = [p for p in poss if p.get("netPos")]
    cmap = _nt_contract_map(base, tok, [p.get("contractId") for p in live])
    rows = []
    for p in live:
        n = float(p.get("netPos") or 0)
        info = cmap.get(p.get("contractId"))
        rows.append({"exchange": "ninjatrader", "symbol": (info[0] if info else str(p.get("contractId"))),
                     "direction": "Long" if n > 0 else "Short", "entry": p.get("netPrice"),
                     "qty": abs(n), "leverage": None, "upnl": None, "mark": None,
                     "margin_mode": None,  # 선물 계좌 — 격리/교차 개념 비적용
                     "point_value": (info[1] if info else None)})
    return rows


ADAPTERS = {
    "bybit": lambda cred, lb: fetch_bybit(cred["key"], cred["secret"], lb),
    "binance": lambda cred, lb: fetch_binance(cred["key"], cred["secret"], lb),
    "gate": lambda cred, lb: fetch_gate(cred["key"], cred["secret"], lb),
    "ninjatrader": fetch_ninjatrader,
}


def _closed_ts(s):
    """'YYYY-MM-DD HH:MM:SS'(UTC 저장 포맷) → unix sec. 파싱 불가면 None."""
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def apply_position_intents(uid, kind, new_rows) -> int:
    """새로 적재된 거래에 '보유 중 적어둔 사전 계획'을 매칭·적용. 반환=적용 건수.
    매칭: (거래소·심볼·방향) 동일 + 계획 작성 시각 < 청산 시각. 같은 키의 청산이 여러 건이면
    가장 이른 청산 1건에만 적용하고 소비(삭제) — 이후 청산엔 새로 적은 계획만 붙는다.
    적용된 거래는 preplanned=1 (자동 매칭 경로 전용 표식 — 사후 기입과 구분)."""
    pints = [p for p in db.get_position_intents(uid) if p["exchange"] == kind]
    if not pints or not new_rows:
        return 0
    applied = 0
    for p in pints:
        cands = []
        for r in new_rows:
            if r.get("symbol") != p["symbol"] or r.get("direction") != p["direction"]:
                continue
            ct = _closed_ts(r.get("closed_at"))
            if ct is not None and p["created"] < ct:
                cands.append((ct, r))
        if not cands:
            continue
        target = min(cands, key=lambda x: x[0])[1]
        fields = {k: p.get(k) for k in ("plan", "setup", "strategy", "sl", "tp", "tp2", "tp3",
                                        "emotion", "memo", "conviction")}
        fields = {k: v for k, v in fields.items() if v not in (None, "")}
        if fields:
            fields["status"] = "기록완료"
            if db.update_intent(uid, target["trade_id"], fields):
                db.mark_preplanned(uid, target["trade_id"])
                applied += 1
        db.delete_position_intent(uid, kind, p["symbol"], p["direction"])
    return applied


def _pull_kind(uid, kind, cred, lookback, wipe_annotated):
    """거래소 하나를 '창 교체' 방식으로 적재 — 재구성이 재풀링마다 포지션 경계를 다르게 잡아
    겹침 중복이 누적되던 문제 차단. 신선한 재구성을 먼저 안전히 수집한 뒤(실패 시 삭제 안 함),
    창 내 자동행을 지우고 새로 넣는다. wipe_annotated=False면 주석 있는 행은 보존(일상 pull),
    True면 창 내 자동행 전부 교체(재적재)."""
    fresh = list(ADAPTERS[kind](cred, lookback))  # 실패하면 예외 → 아래 삭제 안 됨(데이터 보호)
    # 삭제 하한 = 이번에 '실제로 다시 불러온' 거래들의 최초 오픈시각(closed_at과 비교) → 재구성 스팬에
    # 겹치는 조각(청산이 살짝 먼저 끝난 것 포함)은 걷어내되, 그보다 오래된(재조회 못 한) 기록은 절대
    # 안 지운다. 신규 없으면 삭제 안 함(데이터 보호).
    since = min((r.get("opened_at") for r in fresh if r.get("opened_at")), default=None)
    if since:
        db.delete_auto_trades(uid, kind, since=since, unannotated_only=True)  # 항상: 주석 없는 자동행
        if wipe_annotated:  # 재적재: 창 내 자동행 전부 교체
            db.delete_auto_trades(uid, kind, since=since, unannotated_only=False)
    added, new_rows = 0, []
    for r in fresh:
        if db.upsert_trade(uid, r):
            added += 1
            new_rows.append(r)
    preplanned = apply_position_intents(uid, kind, new_rows)
    return {"added": added, "preplanned": preplanned, "error": None}


def pull_user(uid, lookback=None, wipe_annotated=False):
    """거래소별 적재 결과: {exchange: {"added": n, "preplanned": n, "error": str|None}}.
    lookback 지정 시 그 창(재적재는 길게), wipe_annotated=True면 창 내 자동행 전부 교체."""
    lb = lookback or LOOKBACK
    results = {}
    for kind in db.list_connections(uid):
        if kind not in ADAPTERS:
            continue
        cred = db.get_connection(uid, kind)
        try:
            results[kind] = _pull_kind(uid, kind, cred, lb, wipe_annotated)
        except RuntimeError as e:
            logger.warning("pull %s 실패 uid=%s: %s", kind, uid, e)
            results[kind] = {"added": 0, "preplanned": 0, "error": str(e)}
        except Exception:  # noqa: BLE001
            logger.exception("pull %s 예외 uid=%s", kind, uid)
            results[kind] = {"added": 0, "preplanned": 0, "error": f"{kind} 적재 중 알 수 없는 오류"}
    return results


# 재적재(hard resync): 넉넉한 창으로 창 내 자동행을 전부 교체 → 겹침 중복 제거·정확 복원.
# 사전계획·설정은 보존, 창보다 오래된 기록은 그대로 둠(재조회 불가분 보호).
RESYNC_LOOKBACK = int(os.getenv("RESYNC_LOOKBACK_DAYS", "120"))


def resync_user(uid):
    return pull_user(uid, lookback=RESYNC_LOOKBACK, wipe_annotated=True)


# ---------- 보유 중(미청산) 포지션 — 일지엔 닫힌 거래만 들어가므로 현재 보유는 별도 조회 ----------
def _open_positions(kind, key, secret):
    if kind == "bybit":
        ex = ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True})
        poss = ex.fetch_positions(None, {"category": "linear"})
    elif kind == "binance":
        ex = ccxt.binanceusdm({"apiKey": key, "secret": secret, "enableRateLimit": True})
        poss = ex.fetch_positions()
    elif kind == "gate":
        gate_cls = getattr(ccxt, "gate", None) or getattr(ccxt, "gateio")
        ex = gate_cls({"apiKey": key, "secret": secret, "enableRateLimit": True,
                       "options": {"defaultType": "swap", "defaultSettle": "usdt"}})
        poss = ex.fetch_positions(None, {"settle": "usdt"})
    else:
        return []
    rows = []
    for p in poss:
        contracts = _f(p, "contracts")
        if not contracts:
            continue
        side = str(p.get("side") or "").lower()
        sym = (p.get("symbol") or "").split(":")[0].replace("/", "")
        # 마진 모드(격리/교차) — ccxt 통일 필드 우선, 없으면 거래소 원시 필드 폴백
        # (binance: info.marginType / info.isolated, bybit v5: info.tradeMode 0=교차 1=격리).
        # 판별 불가면 None — 모르면서 경고하지 않는다(정직성).
        mm = str(p.get("marginMode") or "").lower() or None
        if mm not in ("isolated", "cross"):
            info = p.get("info") or {}
            if info.get("marginType"):
                mm = str(info["marginType"]).lower()
            elif str(info.get("isolated")).lower() in ("true", "false"):
                mm = "isolated" if str(info["isolated"]).lower() == "true" else "cross"
            elif str(info.get("tradeMode")) in ("0", "1"):
                mm = "isolated" if str(info["tradeMode"]) == "1" else "cross"
            else:
                mm = None
        rows.append({"exchange": kind, "symbol": sym,
                     "direction": "Long" if side == "long" else ("Short" if side == "short" else None),
                     "entry": _f(p, "entryPrice"), "qty": contracts, "leverage": _f(p, "leverage") or None,
                     "upnl": _f(p, "unrealizedPnl"), "mark": _f(p, "markPrice"),
                     "margin_mode": mm})
    return rows


def fetch_open_positions(uid):
    """연결된 거래소의 현재 보유(미청산) 포지션. 일지(닫힌 거래)와 별개의 참고 정보."""
    out = []
    for kind in db.list_connections(uid):
        if kind not in ADAPTERS:
            continue
        cred = db.get_connection(uid, kind)
        try:
            if kind == "ninjatrader":
                out += _nt_open_positions(cred)
            else:
                out += _open_positions(kind, cred["key"], cred["secret"])
        except Exception:  # noqa: BLE001
            logger.warning("보유포지션 %s 조회 실패 uid=%s", kind, uid)
    return out


def _parse(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def analyze_user(uid, be_pct=None):
    trades = db.get_trades(uid)
    rows = [{"실현손익(USDT)": t["pnl"], "상태": t["status"], "심볼": t["symbol"],
             "방향": t["direction"], "청산시각": _parse(t["closed_at"]), "trade_id": t["trade_id"],
             "entry": t["entry"], "qty": t["qty"]} for t in trades]
    summary = behaviors.analyze(rows, be_pct)
    summary = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in summary.items()}
    return summary, trades
