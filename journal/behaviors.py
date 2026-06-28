"""behaviors.py — 일지 행에서 행동 패턴을 결정론적으로 계산 (LLM 없음).

정직성 주의: closed-pnl만으로는 '한 포지션 분할청산'과 '별개 재진입(뇌동/물타기)'을
완전히 구분할 수 없다. 그래서 플래그 문구는 단정하지 않고 'execution 확인 가능'으로 둔다.
진짜 물타기/뇌동 정밀 판정은 거래소 체결(execution/list) 데이터 추가 후.
"""
from collections import Counter
from datetime import datetime


def _pnl(r):
    return r.get("실현손익(USDT)") or 0.0


def analyze(rows: list[dict]) -> dict:
    rows = [r for r in rows if r]
    n = len(rows)
    res = {"n": n, "flags": []}
    if not n:
        return res

    total = sum(_pnl(r) for r in rows)
    wins = sum(1 for r in rows if _pnl(r) > 0)
    res.update(total_pnl=total, wins=wins, win_rate=wins / n)

    unplanned = [r for r in rows if r.get("상태") == "의도 미기입"]
    res["unplanned"] = len(unplanned)
    res["unplanned_pnl"] = sum(_pnl(r) for r in unplanned)
    if unplanned:
        res["flags"].append(f"⚠️ 무계획 진입 {len(unplanned)}건 (합 {res['unplanned_pnl']:+.2f}) — 계획 채워라")

    # 한 종목·방향 집중
    c = Counter((r.get("심볼"), r.get("방향")) for r in rows)
    (sym, dir_), cnt = c.most_common(1)[0]
    if cnt >= 3 and cnt / n >= 0.5:
        res["flags"].append(f"🔁 한 종목 집중: {sym} {dir_ or ''} {cnt}/{n}건")

    # 손실 청산 연속 (시간순)
    ordered = sorted(rows, key=lambda r: r.get("청산시각") or datetime.min)
    streak = mx = 0
    for r in ordered:
        if _pnl(r) < 0:
            streak += 1
            mx = max(mx, streak)
        else:
            streak = 0
    res["losing_streak"] = mx
    if mx >= 3:
        res["flags"].append(f"📉 손실 청산 연속 {mx}")

    # 손실 후 근접(≤30분) 연속 거래 — 분할청산 or 뇌동 (체결데이터로 구분)
    quick = 0
    for a, b in zip(ordered, ordered[1:]):
        if _pnl(a) < 0 and a.get("청산시각") and b.get("청산시각"):
            gap = (b["청산시각"] - a["청산시각"]).total_seconds() / 60
            if 0 <= gap <= 30:
                quick += 1
    res["quick_reentry"] = quick
    if quick >= 2:
        res["flags"].append(f"⚡ 근접(≤30분) 연속 거래 {quick}회 — 분할청산 or 뇌동 (execution으로 구분 가능)")

    return res
