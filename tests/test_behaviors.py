"""behaviors.analyze 정직성 — 승률 분모에서 본절(BE) 제외 + 본절은 연패를 끊지 않음."""
from datetime import datetime

from app import behaviors


def _row(pnl, tid, ts=None, entry=100, qty=1, status="기록완료"):
    return {"실현손익(USDT)": pnl, "상태": status, "심볼": "BTCUSDT", "방향": "Long",
            "청산시각": ts, "trade_id": tid, "entry": entry, "qty": qty}


def test_win_rate_excludes_break_even():
    rows = [_row(50, "w"), _row(-50, "l"), _row(0.05, "be")]  # 명목가 100, 본절밴드 $0.1
    r = behaviors.analyze(rows)
    assert r["wins"] == 1 and r["losses"] == 1 and r["scratch"] == 1
    assert abs(r["win_rate"] - 0.5) < 1e-9  # 1 / (1+1) — 본절 제외


def test_all_break_even_win_rate_zero_not_crash():
    rows = [_row(0.01, "a"), _row(-0.02, "b")]  # 둘 다 본절(명목가 100의 0.1%=$0.1 이내)
    r = behaviors.analyze(rows)
    assert r["wins"] == 0 and r["losses"] == 0 and r["scratch"] == 2
    assert r["win_rate"] == 0.0  # decided=0 → 0, 나눗셈 에러 없음


def test_quick_reentry_ids_collects_follower_trade():
    # 손실(l1) 청산 25분 뒤 청산된 b1이 재진입 후보로 잡힘. 승패 무관(분할/뇌동 구분은 복기 몫)
    rows = [_row(-50, "l1", datetime(2026, 6, 1, 1, 0)),
            _row(30, "b1", datetime(2026, 6, 1, 1, 25)),
            _row(40, "w2", datetime(2026, 6, 1, 3, 0))]
    r = behaviors.analyze(rows)
    assert r["quick_reentry"] == 1
    assert r["quick_reentry_ids"] == ["b1"]


def test_quick_reentry_ids_empty_when_gap_large():
    rows = [_row(-50, "l1", datetime(2026, 6, 1, 1)), _row(30, "b1", datetime(2026, 6, 1, 2))]
    r = behaviors.analyze(rows)
    assert r["quick_reentry"] == 0 and r["quick_reentry_ids"] == []


def test_losing_streak_skips_break_even():
    # 손실 → 본절 → 손실 (시간순). 본절이 연속을 끊지 않아 2연패
    rows = [_row(-50, "l1", datetime(2026, 6, 1, 1)),
            _row(0.01, "be", datetime(2026, 6, 1, 2)),
            _row(-50, "l2", datetime(2026, 6, 1, 3))]
    r = behaviors.analyze(rows)
    assert r["losing_streak"] == 2
