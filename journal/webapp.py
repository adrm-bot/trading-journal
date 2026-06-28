#!/usr/bin/env python3
"""webapp.py — 매매일지 로컬 웹앱 (FastAPI). 키는 기기에만, localhost 전용.

실행:  python -m uvicorn webapp:app --port 8000   (또는 web.bat)
열기:  http://127.0.0.1:8000
"""
import os
import subprocess
import sys

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

import journal_io
import behaviors

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="매매일지")


def _trade_json(r):
    dt = r.get("청산시각")
    return {
        "거래ID": r.get("거래ID"), "거래소": r.get("거래소"), "심볼": r.get("심볼"),
        "방향": r.get("방향"), "진입가": r.get("진입가"), "청산가": r.get("청산가"),
        "수량": r.get("수량"), "실현손익": r.get("실현손익(USDT)"),
        "청산시각": dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "strftime") else None,
        "상태": r.get("상태"), "계획": r.get("계획/의도"), "셋업": r.get("셋업"),
        "무효선": r.get("무효선(SL의도)"), "감정": r.get("감정"), "메모": r.get("메모"),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/data")
def data():
    rows = journal_io.load_journal()
    s = behaviors.analyze(rows)
    s = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}
    return JSONResponse({"summary": s, "trades": [_trade_json(r) for r in rows]})


@app.post("/api/intent")
async def intent(req: Request):
    b = await req.json()
    sl = b.get("무효선")
    try:
        sl = float(sl) if sl not in (None, "") else None
    except (TypeError, ValueError):
        sl = None
    updates = {
        "계획/의도": b.get("계획"), "셋업": b.get("셋업"), "무효선(SL의도)": sl,
        "감정": b.get("감정"), "메모": b.get("메모"), "상태": b.get("상태") or "기록완료",
    }
    return {"ok": bool(journal_io.save_intent(b.get("거래ID"), updates))}


@app.post("/api/pull")
def pull():
    p = subprocess.run([sys.executable, os.path.join(HERE, "pull_trades.py")],
                       capture_output=True, text=True)
    return {"ok": p.returncode == 0, "out": (p.stdout or p.stderr)[-2000:]}
