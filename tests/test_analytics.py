"""analytics.enrich 골든 케이스 — 정직성 직결(R 부호·over_loss 기준)."""
from app import analytics


def test_long_winner_correct_sl():
    t = analytics.enrich({"entry": 100, "exit": 120, "sl": 90, "qty": 1, "direction": "Long"})
    assert t["r"] == 2.0  # (120-100)/(100-90)
    assert t["move_pct"] == 20.0
    assert "sl_invalid" not in t


def test_long_winner_wrongside_sl_no_negative_r():
    # 롱인데 SL을 진입가 위(110)에 오기입 — 수익 거래가 음수 R로 둔갑하면 안 됨
    t = analytics.enrich({"entry": 100, "exit": 120, "sl": 110, "qty": 1, "direction": "Long"})
    assert "r" not in t            # R 미계산
    assert t["sl_invalid"] is True
    assert "stop_violated" not in t  # 규율 계산도 안 돎(오염 차단)


def test_short_winner_correct_sl():
    t = analytics.enrich({"entry": 100, "exit": 80, "sl": 110, "qty": 1, "direction": "Short"})
    assert t["r"] == 2.0  # (100-80)/(110-100)
    assert t["move_pct"] == 20.0
    assert "sl_invalid" not in t


def test_short_wrongside_sl_invalid():
    t = analytics.enrich({"entry": 100, "exit": 80, "sl": 90, "qty": 1, "direction": "Short"})
    assert "r" not in t
    assert t["sl_invalid"] is True


def test_over_loss_is_price_based_not_fee_polluted():
    # 롱 진입 100, 손절 99(리스크 1×100=100), 98.9에 청산(손절 0.1 초과).
    # over_loss = 손절 너머 거리 × 수량 = 0.1*100 = 10 (수수료 무관). pnl에 수수료가 끼어도 영향 없어야.
    t = analytics.enrich({"entry": 100, "exit": 98.9, "sl": 99, "qty": 100,
                          "direction": "Long", "pnl": -118.0})  # pnl엔 수수료 8 포함됐다 가정
    assert t["risk_usd"] == 100.0
    assert t["r"] == -1.1
    assert t["stop_violated"] is True
    assert t["over_loss"] == 10.0  # 118-100=18(잘못) 이 아니라 10(가격기준)


def test_money_left_early_exit():
    t = analytics.enrich({"entry": 100, "exit": 110, "sl": 95, "tp": 120, "qty": 10, "direction": "Long"})
    assert t["r"] == 2.0
    assert t["money_left"] == 100.0  # (120-110)*10


def test_no_sl_no_r():
    t = analytics.enrich({"entry": 100, "exit": 110, "qty": 1, "direction": "Long"})
    assert "r" not in t and "sl_invalid" not in t
    assert t["move_pct"] == 10.0


def test_outcome_break_even_band():
    # 명목가 3000(=100*30), 본절 밴드 0.1% = $3
    assert analytics.outcome(50, 100, 30) == "win"    # 50/3000 = 1.67%
    assert analytics.outcome(2, 100, 30) == "be"      # 2/3000 = 0.067% < 0.1% → 본절
    assert analytics.outcome(-2, 100, 30) == "be"     # 소폭 음수도 본절
    assert analytics.outcome(-50, 100, 30) == "loss"
    assert analytics.outcome(0, 100, 30) == "be"
    assert analytics.outcome(5, None, None) == "win"  # 명목가 모르면 부호로만
    assert analytics.outcome(-5, 0, 0) == "loss"


def test_enrich_sets_outcome():
    assert analytics.enrich({"entry": 100, "exit": 120, "qty": 30, "pnl": 50, "direction": "Long"})["outcome"] == "win"
    assert analytics.enrich({"entry": 100, "exit": 100.05, "qty": 30, "pnl": 1.5, "direction": "Long"})["outcome"] == "be"


def test_csv_cell_defangs_formula_but_keeps_numbers():
    assert analytics.csv_cell("=SUM(A1)") == "'=SUM(A1)"
    assert analytics.csv_cell("+1+1") == "'+1+1"
    assert analytics.csv_cell("@cmd") == "'@cmd"
    assert analytics.csv_cell("-FOO") == "'-FOO"   # 문자열 음수형 심볼 등
    assert analytics.csv_cell(-360.0) == -360.0    # 숫자 음수는 보존(텍스트화 금지)
    assert analytics.csv_cell("추세추종") == "추세추종"
    assert analytics.csv_cell(None) == ""


def test_rr_uses_abs_distance():
    t = analytics.enrich({"entry": 100, "exit": 100, "sl": 95, "tp": 115, "tp2": 130,
                          "qty": 1, "direction": "Long"})
    assert t["rr"] == 3.0    # |115-100|/|100-95|
    assert t["rr2"] == 6.0   # |130-100|/|100-95|
