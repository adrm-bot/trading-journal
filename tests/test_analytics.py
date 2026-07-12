"""analytics.enrich 골든 케이스 — 정직성 직결(R 부호·over_loss 기준)."""
import json

from app import analytics


def test_long_winner_correct_sl():
    t = analytics.enrich({"entry": 100, "exit": 120, "sl": 90, "qty": 1, "direction": "Long"})
    assert t["r"] == 2.0  # (120-100)/(100-90)
    assert t["move_pct"] == 20.0
    assert "sl_invalid" not in t


def test_partial_exit_legs_r():
    # 롱 진입100·SL90(1R=10) · 분할청산 120(+2R)·140(+4R)
    t = analytics.enrich({"entry": 100, "exit": 130, "sl": 90, "qty": 2, "direction": "Long",
                          "exit_legs": "[[120,1],[140,1]]"})
    assert t["legs_r"] == [2.0, 4.0]


def test_partial_exit_legs_r_short():
    # 숏 진입200·SL210(1R=10) · 분할청산 180(+2R)·160(+4R)
    t = analytics.enrich({"entry": 200, "exit": 170, "sl": 210, "qty": 2, "direction": "Short",
                          "exit_legs": "[[180,1],[160,1]]"})
    assert t["legs_r"] == [2.0, 4.0]


def test_mae_mfe_r_long():
    # 롱 진입100·SL90(1R=10)·청산120(+2R) · MAE 93(-0.7R)·MFE 130(+3R) → 못 먹은 1R
    t = analytics.enrich({"entry": 100, "exit": 120, "sl": 90, "qty": 1, "direction": "Long",
                          "mae_price": 93, "mfe_price": 130})
    assert t["r"] == 2.0
    assert t["mae_r"] == -0.7
    assert t["mfe_r"] == 3.0
    assert t["left_on_table_r"] == 1.0


def test_mae_mfe_r_short():
    # 숏 진입200·SL210(1R=10)·청산190(+1R) · MAE 207(-0.7R)·MFE 160(+4R)
    t = analytics.enrich({"entry": 200, "exit": 190, "sl": 210, "qty": 1, "direction": "Short",
                          "mae_price": 207, "mfe_price": 160})
    assert t["mae_r"] == -0.7
    assert t["mfe_r"] == 4.0
    assert t["left_on_table_r"] == 3.0


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


def test_realized_return_uses_ledger_pnl_not_simple_vwap_direction():
    t = analytics.enrich({"entry": 100, "exit": 90, "qty": 10, "pnl": -250, "direction": "Short"})
    assert t["move_pct"] == 10.0       # 가격 VWAP만 보면 숏에 유리
    assert t["pnl_pct"] == -25.0       # 실제 크기 변화가 반영된 실현손익률은 손실


def test_legacy_many_exit_orders_compact_on_enrich_without_losing_raw_count():
    legs = [[100 + i * 0.01, 1] for i in range(40)]
    trade = {"entry": 90, "exit": 100, "qty": 40, "pnl": 400, "direction": "Long",
             "exit_count": 40, "exit_legs": json.dumps(legs)}
    out = analytics.enrich(trade)
    shown = json.loads(out["exit_legs"])
    assert len(shown) <= 12
    assert out["exit_count"] == len(shown)
    assert out["raw_exit_count"] == 40
    assert abs(sum(q for _, q in shown) - 40) < 1e-8


def test_enrich_risk_pct_with_equity():
    t = analytics.enrich({"entry": 100, "exit": 98, "sl": 99, "qty": 100, "direction": "Long", "pnl": -200}, equity=10000)
    assert t["risk_usd"] == 100.0   # |100-99|*100
    assert t["risk_pct"] == 1.0     # 100/10000*100
    t2 = analytics.enrich({"entry": 100, "exit": 98, "sl": 99, "qty": 100, "direction": "Long"})
    assert "risk_pct" not in t2     # 자산 미설정 → 미계산


def test_enrich_custom_be_pct():
    # 명목가 10000, 손익 30 = 0.3%
    assert analytics.enrich({"entry": 100, "exit": 100.3, "qty": 100, "pnl": 30, "direction": "Long"}, be_pct=0.005)["outcome"] == "be"   # <0.5%
    assert analytics.enrich({"entry": 100, "exit": 100.3, "qty": 100, "pnl": 30, "direction": "Long"}, be_pct=0.001)["outcome"] == "win"  # >0.1%


def test_csv_cell_defangs_formula_but_keeps_numbers():
    assert analytics.csv_cell("=SUM(A1)") == "'=SUM(A1)"
    assert analytics.csv_cell("+1+1") == "'+1+1"
    assert analytics.csv_cell("@cmd") == "'@cmd"
    assert analytics.csv_cell("-FOO") == "'-FOO"   # 문자열 음수형 심볼 등
    assert analytics.csv_cell(-360.0) == -360.0    # 숫자 음수는 보존(텍스트화 금지)
    assert analytics.csv_cell("추세추종") == "추세추종"
    assert analytics.csv_cell(None) == ""


def test_sl_direction_error_blocks_wrongside():
    # 저장 전 차단(400)용 — enrich의 sl_ok와 같은 규칙
    assert "롱" in analytics.sl_direction_error("Long", 100, 110)      # 롱인데 SL>진입
    assert "숏" in analytics.sl_direction_error("Short", 100, 90)      # 숏인데 SL<진입
    assert analytics.sl_direction_error("Long", 100, 100) is not None  # SL=진입가(1R=0)도 차단
    assert analytics.sl_direction_error("Long", 100, 90) is None
    assert analytics.sl_direction_error("Short", 100, 110) is None
    assert analytics.sl_direction_error("Long", 100, None) is None     # SL 없음 = 저장 허용(R만 제외)
    assert analytics.sl_direction_error(None, 100, 110) is None        # 방향 미상 → 판정 불가
    assert analytics.sl_direction_error("Long", None, 110) is None     # 진입가 미상 → 판정 불가


def test_point_value_scales_dollar_amounts():
    # NT 선물(MES $5/pt): 리스크·초과손실 달러 환산에 포인트가치 반영, R은 가격 비율이라 무관
    t = analytics.enrich({"entry": 5000, "exit": 4990, "sl": 4995, "qty": 2, "direction": "Long",
                          "pnl": -100, "point_value": 5.0}, equity=10000)
    assert t["risk_usd"] == 50.0    # 5pt × 2계약 × $5
    assert t["risk_pct"] == 0.5
    assert t["over_loss"] == 50.0   # 손절 5pt 초과 × 2 × $5
    assert t["r"] == -2.0
    # point_value 없으면 기존과 동일(×1)
    t2 = analytics.enrich({"entry": 5000, "exit": 4990, "sl": 4995, "qty": 2, "direction": "Long", "pnl": -20})
    assert t2["risk_usd"] == 10.0


def test_rr_uses_abs_distance():
    t = analytics.enrich({"entry": 100, "exit": 100, "sl": 95, "tp": 115, "tp2": 130,
                          "qty": 1, "direction": "Long"})
    assert t["rr"] == 3.0    # |115-100|/|100-95|
    assert t["rr2"] == 6.0   # |130-100|/|100-95|
