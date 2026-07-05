"""regime_bridge.py — 매매일지 ↔ 레짐 분류기(research/regime) 브리지.

- live(): BTCUSDT 15m/1h/4h 라이브 레짐 스냅샷 + MTF 판정 (5분 캐시, 실패해도 앱은 동작)
- perf(rows): 일지 트레이드에 레짐 라벨을 붙여 "레짐별 성과" 집계 — 개인화 게이트의 1단계.
  정직성: 진입시각이 v0 일지에 없어 '청산 시점' 레짐 기준(UI에 명시). R은 무효선(SL의도)·
  진입가·수량이 모두 있는 트레이드만 계산(리스크 = |진입-무효선|×수량).
"""
import os
import sys
import time

import pandas as pd

REGIME_DIR = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "research", "regime"))
if REGIME_DIR not in sys.path:
    sys.path.insert(0, REGIME_DIR)

REGIME_ORDER = ("TREND_UP", "TREND_DOWN", "SQUEEZE", "RANGE", "CHOP")
_live_cache = {"t": 0.0, "data": None}
_label_cache: dict = {}


def supported() -> set:
    """레짐 파케이가 있는 심볼 = 자동 지원. 추가는 research/regime에서
    `python fetch_data.py SYM --start ...` + `python build_dataset.py SYM` 만 돌리면 됨."""
    import glob
    return {os.path.basename(p).replace("_15m.parquet", "")
            for p in glob.glob(os.path.join(REGIME_DIR, "data", "*_15m.parquet"))}


def live(symbol: str = "BTCUSDT", ttl: int = 300) -> dict:
    now = time.time()
    if _live_cache["data"] is not None and now - _live_cache["t"] < ttl:
        return _live_cache["data"]
    try:
        import current  # research/regime 라이브 CLI 로직 재사용
        m = current.fetch_oi(symbol)
        fu = current.fetch_funding(symbol)
        tfs, supers = [], []
        warn = None
        for interval, days in [("15m", 60), ("1h", 80), ("4h", 130)]:
            t = current.classify_tf(symbol, interval, days, m, fu)
            if t is None:
                continue
            r = t.iloc[-1]
            conf = float(r["confidence"])
            hist = t["confidence"].dropna().to_numpy()
            pct = float((hist < conf).mean()) if len(hist) > 100 else None
            grade = None if pct is None else ("높음" if pct >= 0.7 else "보통" if pct >= 0.3 else "낮음")
            supers.append(current.SUPER.get(r["regime"], "NT"))
            tfs.append({"tf": interval, "regime": str(r["regime"]),
                        "name": current.NAME[r["regime"]], "emoji": current.EMOJI[r["regime"]],
                        "conf": round(conf, 2), "grade": grade,
                        "ts": t.index[-1].strftime("%m-%d %H:%M")})
            if interval == "15m" and len(hist) > 500:
                import numpy as np
                if conf < float(np.quantile(hist[-2880:], 0.2)):
                    warn = "확신이 최근 30일 하위 20% — 성격 전환 가능성, 사이즈 축소"
        if len(supers) == 3:
            if len(set(supers)) == 1 and supers[0] != "NT":
                verdict = f"세 TF 모두 {'상승' if supers[0] == 'UP' else '하락'} 방향 — 강한 합의"
            elif len(set(supers)) == 1:
                verdict = "세 TF 모두 비추세 — 방향 베팅 근거 없음"
            elif supers[1] == supers[2] and supers[1] != "NT":
                d = "상승" if supers[1] == "UP" else "하락"
                verdict = f"상위(1h·4h) {d} 일치 — 방향 우세, 15m은 진입 타이밍 대기"
            elif supers[0] == supers[1] and supers[0] != "NT":
                verdict = "단기(15m·1h) 일치, 4h 불일치 — 부분 합의, 사이즈 보수"
            else:
                verdict = "혼조 — TF 간 성격 불일치, 보수적으로"
        else:
            verdict = "데이터 부족"
        data = {"symbol": symbol, "tfs": tfs, "verdict": verdict, "warn": warn,
                "asof": time.strftime("%H:%M:%S")}
        _live_cache.update(t=now, data=data)
        return data
    except Exception as e:  # noqa: BLE001 — 대시보드는 레짐 실패에도 떠야 함
        return {"error": f"레짐 스냅샷 실패: {e}"}


def _norm_sym(s) -> str | None:
    if not s:
        return None
    s = str(s).upper().replace("/", "").split(":")[0]
    return s if s in supported() else None


def _labels(sym: str):
    """15m 레짐 라벨 시계열 (프로세스 캐시). 파케이 없으면 None."""
    if sym in _label_cache:
        return _label_cache[sym]
    path = os.path.join(REGIME_DIR, "data", f"{sym}_15m.parquet")
    if not os.path.exists(path):
        _label_cache[sym] = None
        return None
    import classifier
    import features
    df = pd.read_parquet(path)
    res = classifier.classify(features.compute(df))
    lab = res["regime"].iloc[features.DEFAULT_FP.burn_in:]
    _label_cache[sym] = lab
    return lab


def perf(rows: list) -> dict:
    """레짐별 성과 집계. 청산시각(UTC-naive)을 그 시점의 15m 레짐에 매칭."""
    import current
    agg: dict = {}
    unmatched = 0
    for r in rows or []:
        sym = _norm_sym(r.get("심볼"))
        dt = r.get("청산시각")
        if not sym or dt is None:
            unmatched += 1
            continue
        lab = _labels(sym)
        if lab is None:
            unmatched += 1
            continue
        ts = pd.Timestamp(dt).tz_localize("UTC")
        i = lab.index.searchsorted(ts, side="right") - 1
        if i < 0 or (ts - lab.index[i]) > pd.Timedelta(minutes=30):
            unmatched += 1
            continue
        reg = lab.iloc[i]
        if pd.isna(reg):
            unmatched += 1
            continue
        a = agg.setdefault(str(reg), {"n": 0, "pnl": 0.0, "wins": 0, "rs": []})
        pnl = float(r.get("실현손익(USDT)") or 0.0)
        a["n"] += 1
        a["pnl"] += pnl
        a["wins"] += 1 if pnl > 0 else 0
        try:
            entry = float(r.get("진입가"))
            sl = float(r.get("무효선(SL의도)"))
            qty = float(r.get("수량"))
            risk = abs(entry - sl) * qty
            if risk > 0:
                a["rs"].append(pnl / risk)
        except (TypeError, ValueError):
            pass
    table = []
    for reg in REGIME_ORDER:
        if reg not in agg:
            continue
        a = agg[reg]
        table.append({
            "regime": reg, "name": current.NAME[reg], "emoji": current.EMOJI[reg],
            "n": a["n"], "pnl": round(a["pnl"], 2),
            "win_rate": round(a["wins"] / a["n"], 2) if a["n"] else None,
            "avg_R": round(sum(a["rs"]) / len(a["rs"]), 2) if a["rs"] else None,
            "n_R": len(a["rs"]),
        })
    syms = ", ".join(sorted(s.replace("USDT", "") for s in supported())) or "없음"
    return {"rows": table, "unmatched": unmatched,
            "note": f"청산 시점 레짐 기준(진입시각 미수집 v0) · 지원 심볼 {syms} · "
                    "R은 무효선·진입가·수량이 있는 트레이드만"}
