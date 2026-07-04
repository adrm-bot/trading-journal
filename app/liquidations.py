"""liquidations.py — 실시간 청산 '체결' 수집(공개 WS · 무키).

주의: 이건 코인글래스/KingFisher식 **예측형 청산 히트맵(자석 구간)이 아니다.**
거래소가 실제로 강제청산한 주문을 실시간으로 받아 '가동 이후' 롤링 버퍼에 쌓는 것 —
정직성: 앞으로 어디서 청산될지가 아니라, 방금 무엇이 청산됐는지(실제 체결)만 말한다.

Bybit 공개 WS 우선(앱의 리전 패턴), 실패 시 Binance USDⓈ-M. 상시 asyncio 태스크.
websockets 미설치·연결 실패·라이브러리 예외는 전부 삼켜서 available:false로 조용히 비활성
(대시보드를 절대 죽이지 않는다). 롱/숏 매핑은 거래소 필드 의미라 라이브 검증 대상(베타).
"""
import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque

logger = logging.getLogger("app.liquidations")

# 시장 전반 감(BTC 중심) — Bybit는 심볼별 토픽 구독이라 소수 메이저만. 대시보드 시장맥락은 글로벌.
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "LIQ_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT").split(",") if s.strip()]
MAP_SYMBOL = os.getenv("LIQ_MAP_SYMBOL", "BTCUSDT").upper()
_MAXLEN = int(os.getenv("LIQ_BUFFER", "12000"))  # 롤링 버퍼 상한(메모리 미미)
_DISABLED = os.getenv("LIQ_STREAM_DISABLED", "false").lower() == "true"

_BUF: "deque[dict]" = deque(maxlen=_MAXLEN)
_STATE = {"started_at": None, "source": None, "connected": False, "last_msg_at": None}


def _push(sym, side, price, qty, t_ms):
    """side: 'long'|'short' (청산된 포지션 방향). notional = 가격×수량."""
    if not price or not qty:
        return
    _BUF.append({"t": int(t_ms), "sym": sym, "side": side,
                 "price": float(price), "qty": float(qty),
                 "notional": round(float(price) * float(qty), 2)})
    _STATE["last_msg_at"] = int(time.time() * 1000)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---- 거래소별 메시지 정규화 (필드 의미는 라이브 검증 대상 — 방어적으로) ----
def parse_bybit(msg):
    """Bybit v5 allLiquidation.{sym}: data[].{T,s,S,v,p}. S=청산 주문 방향 →
    Sell=롱 포지션 청산, Buy=숏 포지션 청산 (청산은 반대 주문으로 닫힘)."""
    out = []
    for d in (msg.get("data") or []):
        s = str(d.get("S") or d.get("side") or "").lower()
        side = "long" if s.startswith("sell") else ("short" if s.startswith("buy") else None)
        if side is None:
            continue
        out.append((str(d.get("s") or d.get("symbol") or ""), side,
                    _f(d.get("p") or d.get("price")), _f(d.get("v") or d.get("size") or d.get("q")),
                    int(d.get("T") or d.get("t") or msg.get("ts") or time.time() * 1000)))
    return out


def parse_binance(msg):
    """Binance !forceOrder@arr: {data:{o:{s,S,p,q,ap,T}}} 또는 {o:{...}}.
    S=SELL → 롱 청산, BUY → 숏 청산."""
    o = ((msg.get("data") or msg).get("o")) or {}
    s = str(o.get("S") or "").lower()
    side = "long" if s == "sell" else ("short" if s == "buy" else None)
    if side is None:
        return []
    return [(str(o.get("s") or ""), side, _f(o.get("ap") or o.get("p")), _f(o.get("q")),
             int(o.get("T") or time.time() * 1000))]


async def _run_bybit():
    import websockets  # uvicorn[standard] 동봉 (미설치면 ImportError → 상위에서 폴백)
    url = "wss://stream.bybit.com/v5/public/linear"
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": [f"allLiquidation.{s}" for s in SYMBOLS]}))
        _STATE.update(source="bybit", connected=True)
        logger.info("liq: bybit 구독 %s", SYMBOLS)
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if str(msg.get("topic") or "").startswith("allLiquidation"):
                for sym, side, price, qty, t in parse_bybit(msg):
                    _push(sym, side, price, qty, t)


async def _run_binance():
    import websockets
    url = "wss://fstream.binance.com/stream?streams=" + "/".join(f"{s.lower()}@forceOrder" for s in SYMBOLS)
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
        _STATE.update(source="binance", connected=True)
        logger.info("liq: binance 구독 %s", SYMBOLS)
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            for sym, side, price, qty, t in parse_binance(msg):
                _push(sym, side, price, qty, t)


async def run_collector():
    """상시 수집 루프 — Bybit 우선, 실패 시 Binance. 재접속 백오프. 앱 종료 시 취소."""
    if _DISABLED:
        logger.info("liq: LIQ_STREAM_DISABLED — 수집 비활성")
        return
    _STATE["started_at"] = int(time.time() * 1000)
    backoff = 2
    while True:
        for runner in (_run_bybit, _run_binance):
            try:
                await runner()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — ImportError·연결끊김·차단 등, 다음 소스/재시도
                _STATE["connected"] = False
                logger.warning("liq: %s 스트림 실패(%s) — 폴백/재시도", runner.__name__, type(e).__name__)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)  # 지수 백오프(최대 60s)


# ---- 집계(순수 함수 — 네트워크 비의존, 단위테스트 가능) ----
def _aggregate(events, now_ms, window_min=60, map_symbol=MAP_SYMBOL, nbins=22):
    win = window_min * 60_000
    recent = [e for e in events if now_ms - e["t"] <= win]
    longs = round(sum(e["notional"] for e in recent if e["side"] == "long"), 2)
    shorts = round(sum(e["notional"] for e in recent if e["side"] == "short"), 2)
    tape = sorted(recent, key=lambda e: e["notional"], reverse=True)[:6]
    tape = [{"sym": e["sym"], "side": e["side"], "notional": e["notional"],
             "price": e["price"], "t": e["t"]} for e in tape]
    # 가격대별 청산 $ (map_symbol) — 실제 체결로 만든 미니 맵(예측 아님)
    mp = [e for e in recent if e["sym"] == map_symbol]
    bins, price = [], (mp[-1]["price"] if mp else None)
    if len(mp) >= 3:
        lo, hi = min(e["price"] for e in mp), max(e["price"] for e in mp)
        if hi > lo:
            w = (hi - lo) / nbins
            agg = [{"lo": lo + i * w, "hi": lo + (i + 1) * w, "long": 0.0, "short": 0.0} for i in range(nbins)]
            for e in mp:
                idx = min(nbins - 1, int((e["price"] - lo) / w))
                agg[idx][e["side"]] += e["notional"]
            bins = [{"lo": round(b["lo"], 2), "hi": round(b["hi"], 2),
                     "long": round(b["long"], 2), "short": round(b["short"], 2)}
                    for b in agg if b["long"] or b["short"]]
    return {"longs_usd": longs, "shorts_usd": shorts, "n": len(recent), "tape": tape,
            "map": {"symbol": map_symbol, "price": price, "bins": bins}}


def snapshot(window_min=60):
    now = int(time.time() * 1000)
    started = _STATE["started_at"]
    if not started:
        return {"available": False, "reason": "청산 스트림 미시작"}
    up_min = max(0, (now - started) // 60_000)
    data = _aggregate(list(_BUF), now, window_min)
    if not _BUF and not _STATE["connected"]:
        return {"available": False, "reason": "청산 스트림 연결 대기", "source": _STATE["source"]}
    data.update(available=True, source=_STATE["source"], connected=_STATE["connected"],
                since_min=min(window_min, up_min), window_min=window_min, symbols=SYMBOLS)
    return data


_TASK = None


def start(loop=None):
    """FastAPI startup에서 호출 — 백그라운드 수집 태스크 1개 생성(중복 방지)."""
    global _TASK
    if _TASK and not _TASK.done():
        return _TASK
    _TASK = asyncio.ensure_future(run_collector())
    return _TASK


async def stop():
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _TASK
    _TASK = None
