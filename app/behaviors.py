"""behaviors.py — 일지 행에서 행동 패턴 계산 (app 자립 — journal/ 의존 제거).

정직성: closed-pnl만으로는 분할청산과 별개 재진입을 완전 구분 못 함.
플래그는 단정하지 않고 'execution 확인 가능'으로 둔다.
"""
from collections import Counter
from datetime import datetime

from . import analytics


def _pnl(r):
    return r.get("실현손익(USDT)") or 0.0


def analyze(rows: list[dict], be_pct=None) -> dict:
    rows = [r for r in rows if r]
    n = len(rows)
    res = {"n": n, "flags": []}
    if not n:
        return res

    bp = be_pct if be_pct is not None else analytics.BE_PCT
    for r in rows:  # 단일 판정(승/패/본절) — analytics.outcome으로 통일
        r["_oc"] = analytics.outcome(_pnl(r), r.get("entry"), r.get("qty"), bp)
    total = sum(_pnl(r) for r in rows)
    wins = sum(1 for r in rows if r["_oc"] == "win")
    losses = sum(1 for r in rows if r["_oc"] == "loss")
    decided = wins + losses  # 승률 분모에서 본절 제외 — 본절이 패배로 둔갑하지 않게
    res.update(total_pnl=total, wins=wins, losses=losses, scratch=n - wins - losses,
               win_rate=(wins / decided if decided else 0.0))

    unplanned = [r for r in rows if r.get("상태") == "의도 미기입"]
    res["unplanned"] = len(unplanned)
    res["unplanned_pnl"] = sum(_pnl(r) for r in unplanned)
    if unplanned:
        res["flags"].append({"level": "warn", "text": f"계획 미기입 {len(unplanned)}건 (합 {res['unplanned_pnl']:+.2f} USDT) — 사후 계획을 채워보세요"})

    # 한 종목·방향 집중 (방향 None은 종목만으로)
    c = Counter((r.get("심볼"), r.get("방향")) for r in rows)
    (sym, dir_), cnt = c.most_common(1)[0]
    if cnt >= 3 and cnt / n >= 0.5:
        label = f"{sym} {dir_}".strip() if dir_ else f"{sym}"
        res["flags"].append({"level": "info", "text": f"한 종목 집중 — {label} {cnt}/{n}건"})

    # 손실 청산 연속 (시간순, 안정 정렬: 시각→심볼→trade_id)
    ordered = sorted(rows, key=lambda r: (r.get("청산시각") or datetime.min, r.get("심볼") or "", r.get("거래ID") or r.get("trade_id") or ""))
    streak = mx = 0
    for r in ordered:
        if r["_oc"] == "loss":
            streak += 1
            mx = max(mx, streak)
        elif r["_oc"] == "win":
            streak = 0
        # 본절(be)은 연속을 끊지도 잇지도 않음(스킵)
    res["losing_streak"] = mx
    if mx >= 3:
        res["flags"].append({"level": "warn", "text": f"손실 청산 {mx}연속 — 과매매·복수매매 주의"})

    # 손실 후 근접(≤30분) 연속 거래 — 분할청산 or 뇌동
    quick = 0
    for a, b in zip(ordered, ordered[1:]):
        if a["_oc"] == "loss" and a.get("청산시각") and b.get("청산시각"):
            gap = (b["청산시각"] - a["청산시각"]).total_seconds() / 60
            if 0 <= gap <= 30:
                quick += 1
    res["quick_reentry"] = quick
    if quick >= 2:
        res["flags"].append({"level": "info", "text": f"근접(≤30분) 연속 거래 {quick}회 — 분할청산인지 충동매매인지 점검"})

    return res
