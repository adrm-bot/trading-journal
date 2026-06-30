"""tvremix.py — TVRemix MCP(Streamable HTTP) 클라이언트. TradingView OHLCV 등.

- 키는 TVREMIX_API_KEY 환경변수에서만 읽음(코드/깃 비포함). 키 없으면 enabled()=False → 호출부가 기존 폴백.
- stateless: tools/call 1회 = HTTP POST 1번(init 핸드셰이크 불필요). 레이트리밋 20/분·200/시간·1500/일.
- 모든 실패(키없음·HTTP·타임아웃·429·파싱)는 None/[]로 graceful — 대시보드 절대 안 죽임.
"""
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("app.tvremix")
_URL = "https://tvremix.xyz/api/mcp/v1"


def enabled():
    return bool(os.environ.get("TVREMIX_API_KEY"))


def _call(name, args, timeout=12):
    key = os.environ.get("TVREMIX_API_KEY")
    if not key:
        return None
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}
    req = urllib.request.Request(
        _URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": "Bearer " + key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (고정 https)
            raw = r.read().decode()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("tvremix %s 실패: %s", name, e)
        return None
    res = None
    try:
        res = json.loads(raw)
    except ValueError:  # SSE(text/event-stream) 폴백: 마지막 data: 라인 파싱
        for line in reversed(raw.splitlines()):
            if line.startswith("data:"):
                try:
                    res = json.loads(line[5:].strip())
                    break
                except ValueError:
                    continue
    if not (res and res.get("result") and res["result"].get("content")):
        return None
    try:
        return json.loads(res["result"]["content"][0]["text"])
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def ohlcv(symbol, interval="1D", count=300):
    """ccxt 형식 봉 리스트 [{t,o,h,l,c,v}, ...](오래된→최신). 실패 시 []."""
    d = _call("get_ohlcv", {"symbol": symbol, "interval": interval, "count": count})
    return (d or {}).get("bars") or []


def closes(symbol, interval="1D", count=300):
    """종가 리스트(오래된→최신). 실패 시 []."""
    return [b["c"] for b in ohlcv(symbol, interval, count) if b and b.get("c") is not None]
