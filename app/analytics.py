"""analytics.py — 거래 행 파생값 계산 (순수 함수: 부팅·DB·네트워크 비의존 → 단위테스트 가능).

정직성 규칙(핵심 가치):
- R은 손절(SL)이 진입 대비 올바른 방향일 때만 계산. 반대편 SL(오기입: 롱인데 SL>진입 등)은
  R을 비워두고 sl_invalid 플래그만 둔다 — 수익 거래가 음수 R로 둔갑해 평균R·히스토그램을 오염시키는 것 방지.
- over_loss(스탑 미준수 초과손실)는 '손절 너머로 더 간 거리 × 수량'(가격 기준, 수수료 불포함)으로
  risk_usd와 동일한 기준에서 계산. (실현손익은 수수료·펀딩 포함이라 risk와 섞으면 구조적으로 과대.)
"""
import json
import os
from datetime import datetime

# 본절(break-even) 밴드: |실현손익| ÷ 명목가(진입×수량)가 이 비율 미만이면 무승부로 본다.
# 기본 0.1% ≈ 선물 왕복 수수료 크기 — '거래비용보다 작은 결과 = 사실상 본절'. env BREAK_EVEN_PCT(%)로 조정.
BE_PCT = float(os.getenv("BREAK_EVEN_PCT", "0.1")) / 100.0
EXIT_LEG_MERGE_BPS = float(os.getenv("EXIT_LEG_MERGE_BPS", "2"))
EXIT_LEG_MAX_BANDS = int(os.getenv("EXIT_LEG_MAX_BANDS", "12"))


def _compact_legs(legs, merge_bps=EXIT_LEG_MERGE_BPS, max_bands=EXIT_LEG_MAX_BANDS):
    """과거 DB의 과다 청산 주문도 화면 계산 시 동일 가격대 VWAP 구간으로 정규화한다."""
    try:
        clean = [(float(p), float(q)) for p, q in (legs or []) if p and q and float(q) > 0]
    except (TypeError, ValueError):
        return []
    merged = []
    for price, qty in clean:
        if merged:
            pp, pq = merged[-1]
            bps = abs(price - pp) / max(abs(price), abs(pp), 1e-12) * 10_000
            if bps <= merge_bps:
                nq = pq + qty
                merged[-1] = ((pp * pq + price * qty) / nq, nq)
                continue
        merged.append((price, qty))
    max_bands = max(1, int(max_bands))
    if len(merged) > max_bands:
        size = (len(merged) + max_bands - 1) // max_bands
        compact = []
        for i in range(0, len(merged), size):
            chunk = merged[i:i + size]
            qty = sum(q for _, q in chunk)
            compact.append((sum(p * q for p, q in chunk) / qty, qty))
        merged = compact
    return [[round(p, 10), round(q, 10)] for p, q in merged]


def outcome(pnl, entry, qty, be_pct=BE_PCT):
    """승/패/본절 판정. 명목가 대비 손익이 밴드 이내면 'be'(본절). 명목가 모르면 부호로만."""
    p = pnl or 0.0
    notional = abs((entry or 0) * (qty or 0))
    if notional > 0 and abs(p) < be_pct * notional:
        return "be"
    if p > 0:
        return "win"
    if p < 0:
        return "loss"
    return "be"


def csv_cell(v):
    """CSV 수식 인젝션 방어 — 문자열이 =,+,-,@,탭,CR로 시작하면 앞에 ' 붙임. 숫자는 그대로(음수 보존)."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def sl_direction_error(direction, entry, sl):
    """반대편 SL 오기입 검증 — 저장 전 차단용(enrich의 sl_ok와 같은 규칙, 진입가=SL도 차단: 1R=0).
    문제면 사용자 메시지, 정상·판정불가(방향/진입가 미상)면 None."""
    if sl is None or not entry or direction not in ("Long", "Short"):
        return None
    short = direction == "Short"
    if (short and sl <= entry) or (not short and sl >= entry):
        base = "숏은 손절가가 진입가보다 높아야 합니다" if short else "롱은 손절가가 진입가보다 낮아야 합니다"
        return base + " · 손절가를 다시 확인해 주세요"
    return None


def enrich(t: dict, equity=None, be_pct=BE_PCT) -> dict:
    """거래에 가격변동%·실현손익률·R·계획 R:R·리스크(·계좌%)·보유시간·규율 파생값 추가.
    equity: 계좌 자산(USDT) — 주면 risk_pct(계좌 대비 리스크%) 계산. be_pct: 본절 밴드."""
    # 과거 버전이 저장한 수십~수백 청산 주문도 재적재를 기다리지 않고 읽기 쉬운 가격 구간으로
    # 변환한다. 원시 주문 수는 raw_exit_count에 남겨 데이터가 사라진 것처럼 보이지 않게 한다.
    if t.get("exit_legs"):
        try:
            raw_legs = json.loads(t["exit_legs"]) if isinstance(t["exit_legs"], str) else t["exit_legs"]
            compact = _compact_legs(raw_legs)
            if compact:
                raw_count = max(int(t.get("raw_exit_count") or 0), int(t.get("exit_count") or 0),
                                len(raw_legs or []))
                if raw_count > len(compact):
                    t["raw_exit_count"] = raw_count
                t["exit_count"] = len(compact)
                t["exit_legs"] = json.dumps(compact)
        except (ValueError, TypeError):
            pass
    e, x, sl, d, qty = t.get("entry"), t.get("exit"), t.get("sl"), t.get("direction"), t.get("qty")
    tp, tp2, tp3 = t.get("tp"), t.get("tp2"), t.get("tp3")
    short = d == "Short"
    t["outcome"] = outcome(t.get("pnl"), e, qty, be_pct)  # win/loss/be (본절 밴드)
    if e and x and e != 0:
        t["move_pct"] = round(((x - e) / e * 100) * (-1 if short else 1), 2)
    if e and x and sl and e != sl:
        sl_ok = (sl < e) if not short else (sl > e)  # 롱: SL<진입, 숏: SL>진입이어야 유효
        if sl_ok:
            t["r"] = round((e - x) / (sl - e) if short else (x - e) / (e - sl), 2)
        else:
            t["sl_invalid"] = True  # 반대편 SL 오기입 — R 미계산(통계 오염 방지)
    if e and sl and tp and e != sl:
        t["rr"] = round(abs(tp - e) / abs(e - sl), 2)
    if e and sl and tp2 and e != sl:
        t["rr2"] = round(abs(tp2 - e) / abs(e - sl), 2)
    if e and sl and tp3 and e != sl:
        t["rr3"] = round(abs(tp3 - e) / abs(e - sl), 2)
    pv = t.get("point_value") or 1.0  # 선물 포인트가치(NT) — 크립토 선형은 1
    # 스케일 인/아웃 거래는 단순 진입VWAP→청산VWAP 방향과 실제 손익 부호가 다를 수 있다.
    # 카드의 대표 수익률은 거래소 실현손익 ÷ 누적 진입 명목가로 계산해 손익과 항상 정합시킨다.
    entry_notional = abs((e or 0) * (qty or 0) * pv)
    if entry_notional > 0 and t.get("pnl") is not None:
        t["pnl_pct"] = round(float(t["pnl"]) / entry_notional * 100, 2)
    if e and sl and qty:
        t["risk_usd"] = round(abs(e - sl) * qty * pv, 2)  # 계획 리스크 = 진입~손절 거리 × 수량 × 포인트가치
        if equity and equity > 0:
            t["risk_pct"] = round(t["risk_usd"] / equity * 100, 2)  # 계좌 대비 리스크%
    oa, ca = t.get("opened_at"), t.get("closed_at")
    if oa and ca:
        try:
            t["hold_min"] = round((datetime.fromisoformat(ca) - datetime.fromisoformat(oa)).total_seconds() / 60)
        except (TypeError, ValueError):
            pass
    # 규율: 계획(예상) SL/TP vs 실제 청산 대조 (R이 유효 계산된 거래만)
    r = t.get("r")
    if e and x and sl and r is not None:
        if r < 0:  # 손실: 계획 손절 너머로 청산했나(스탑 미준수)
            violated = (x < sl) if not short else (x > sl)
            t["stop_violated"] = bool(violated)
            if violated and qty:
                t["over_loss"] = round(abs(x - sl) * qty * pv, 2)  # 손절 너머 거리 × 수량 × 포인트가치
        elif r > 0 and tp:  # 이익: 계획 익절 못 미쳐 조기청산했나
            early = (x < tp) if not short else (x > tp)
            if early and qty:
                t["money_left"] = round(abs(tp - x) * qty * pv, 2)  # 테이블에 두고 온 이익
    if t.get("rr"):
        t["exit_eff"] = round(r / t["rr"], 2) if r is not None else None
    # 분할청산 레그별 R(가격 기준) — SL 유효 + 레그 보존(워크 재구성)된 거래만. 분할익절 충실도 가시화.
    if e and sl and e != sl and t.get("exit_legs"):
        try:
            legs = json.loads(t["exit_legs"]) if isinstance(t["exit_legs"], str) else t["exit_legs"]
            rd = abs(e - sl)
            t["legs_r"] = [round(((p - e) / rd) * (-1 if short else 1), 2) for p, q in legs if p]
        except (ValueError, TypeError):
            pass
    # MAE/MFE의 R 환산 + 'MFE 대비 못 먹은 R'(청산 충실도) — SL 유효할 때
    if e and sl and e != sl:
        rd = abs(e - sl)
        mae_p, mfe_p = t.get("mae_price"), t.get("mfe_price")
        if mae_p is not None:  # 최대 역행(보통 음수 R)
            t["mae_r"] = round((mae_p - e) / rd * (-1 if short else 1), 2)
        if mfe_p is not None:  # 최대 순행(보통 양수 R)
            t["mfe_r"] = round((mfe_p - e) / rd * (-1 if short else 1), 2)
            if r is not None:
                t["left_on_table_r"] = round(t["mfe_r"] - r, 2)  # 먹을 수 있었는데 못 먹은 R
    return t
