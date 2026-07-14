"""reconstruct_walk(Binance/Gate 공용) + _finalize_pos 골든 케이스."""
import pytest

from app import engine
from conftest import fill, ts, assert_pnl_sum, T0

ROUND = 1e-8


def test_single_open_close_long():
    trades = [fill(1, "BUY", 100, 2, T0, pnl=0.0, fee=0.1),
              fill(2, "SELL", 110, 2, T0 + 3600_000, pnl=20.0, fee=0.11)]
    out = engine.reconstruct_walk("binance", "BTCUSDT", trades)
    assert len(out) == 1
    r = out[0]
    assert r["direction"] == "Long"
    assert r["entry"] == 100.0 and r["exit"] == 110.0
    assert r["qty"] == 2.0
    assert r["pnl"] == 20.0
    assert abs(r["fees"] - 0.21) < ROUND
    assert r["fill_count"] == 2
    assert r["liquidated"] == 0
    assert r["trade_id"] == "binance:pos:BTCUSDT:2"
    assert r["opened_at"] == ts(T0) and r["closed_at"] == ts(T0 + 3600_000)


def test_scale_in_vwap_entry():
    trades = [fill(1, "BUY", 100, 1, T0),
              fill(2, "BUY", 200, 1, T0 + 60_000),
              fill(3, "SELL", 160, 2, T0 + 120_000, pnl=20.0)]
    out = engine.reconstruct_walk("binance", "ETHUSDT", trades)
    assert len(out) == 1
    assert out[0]["entry"] == 150.0
    assert out[0]["exit"] == 160.0
    assert out[0]["qty"] == 2.0
    assert out[0]["pnl"] == 20.0


def test_scale_out_vwap_exit():
    trades = [fill(1, "BUY", 100, 2, T0),
              fill(2, "SELL", 120, 1, T0 + 60_000, pnl=20.0),
              fill(3, "SELL", 140, 1, T0 + 120_000, pnl=40.0)]
    out = engine.reconstruct_walk("binance", "ETHUSDT", trades)
    assert len(out) == 1
    assert out[0]["entry"] == 100.0
    assert out[0]["exit"] == 130.0
    assert out[0]["qty"] == 2.0
    assert out[0]["pnl"] == 60.0
    assert out[0]["fill_count"] == 3
    assert out[0]["exit_count"] == 2  # 분할청산 2레그
    import json as _json
    assert _json.loads(out[0]["exit_legs"]) == [[120.0, 1.0], [140.0, 1.0]]


def test_exit_legs_merged_by_order():
    """한 청산 주문이 수십 체결로 쪼개져 들어와도 레그는 주문 단위로 1개(vwap). P&L·수량 불변."""
    trades = [fill(1, "BUY", 100, 4, T0, order="o1")] + [
        fill(10 + i, "SELL", 120 + i, 1, T0 + 60_000 + i, pnl=20.0 + i, order="o2") for i in range(4)]
    out = engine.reconstruct_walk("binance", "ETHUSDT", trades)
    assert len(out) == 1
    r = out[0]
    assert r["qty"] == 4.0
    assert r["exit_count"] == 1        # 4체결 → 1주문 → 1레그
    assert r["exit_legs"] is None      # 단일 레그는 분할 상세 미저장(설계)
    assert r["exit"] == 121.5          # vwap((120+121+122+123)/4)
    assert abs(r["pnl"] - (20 + 21 + 22 + 23)) < ROUND   # P&L=realizedPnl 합(병합 무관)


def test_exit_legs_distinct_orders_kept():
    """서로 다른 청산 주문은 별도 레그로 유지(진짜 분할청산)."""
    trades = [fill(1, "BUY", 100, 2, T0),
              fill(2, "SELL", 120, 1, T0 + 60_000, pnl=20.0, order="oA"),
              fill(3, "SELL", 140, 1, T0 + 120_000, pnl=40.0, order="oB")]
    out = engine.reconstruct_walk("binance", "ETHUSDT", trades)
    assert out[0]["exit_count"] == 2


def test_exit_legs_near_same_price_compacted_across_orders():
    """수십 개 주문이어도 같은 가격대면 화면에는 하나의 청산 구간으로 보인다."""
    trades = [fill(1, "BUY", 100, 40, T0, order="open")]
    trades += [fill(10 + i, "SELL", 120 + (i % 2) * 0.001, 1,
                    T0 + 60_000 + i, pnl=20.0, order=f"exit-{i}") for i in range(40)]
    out = engine.reconstruct_walk("binance", "ETHUSDT", trades)
    assert out[0]["fill_count"] == 41       # 원시 체결 감사값은 보존
    assert out[0]["exit_count"] == 1        # 화면용 의미 구간은 1개
    assert out[0]["raw_exit_count"] == 40
    assert out[0]["exit_legs"] is not None   # 원시 주문→가격구간 압축 사실은 감사용으로 보존


def test_exit_legs_many_prices_capped_and_qty_conserved():
    legs = [(100 + i, 1 + (i % 3)) for i in range(50)]
    compact = engine._compact_exit_legs(legs, merge_bps=0, max_bands=12)
    assert len(compact) <= 12
    assert abs(sum(q for _, q in compact) - sum(q for _, q in legs)) < ROUND


def test_flip_long_to_short():
    trades = [fill(1, "BUY", 100, 2, T0),
              fill(2, "SELL", 110, 3, T0 + 60_000, pnl=20.0),
              fill(3, "BUY", 90, 1, T0 + 120_000, pnl=20.0)]
    out = engine.reconstruct_walk("binance", "ETHUSDT", trades)
    assert len(out) == 2
    long_pos, short_pos = out[0], out[1]
    assert long_pos["direction"] == "Long" and long_pos["entry"] == 100.0 and long_pos["exit"] == 110.0 and long_pos["qty"] == 2.0
    assert long_pos["trade_id"] == "binance:pos:ETHUSDT:2"
    assert short_pos["direction"] == "Short" and short_pos["entry"] == 110.0 and short_pos["exit"] == 90.0 and short_pos["qty"] == 1.0
    assert short_pos["trade_id"] == "binance:pos:ETHUSDT:3"


def test_in_progress_excluded():
    trades = [fill(1, "BUY", 100, 2, T0)]
    assert engine.reconstruct_walk("binance", "BTCUSDT", trades) == []


def test_partial_then_open_excluded():
    trades = [fill(1, "BUY", 100, 3, T0), fill(2, "SELL", 110, 1, T0 + 60_000, pnl=10.0)]
    assert engine.reconstruct_walk("binance", "BTCUSDT", trades) == []


def test_vwap_pnl_fallback():
    trades = [fill(1, "BUY", 100, 2, T0, pnl=0.0, fee=0.05),
              fill(2, "SELL", 120, 2, T0 + 60_000, pnl=0.0, fee=0.07)]
    out = engine.reconstruct_walk("gate", "BTC_USDT", trades)
    assert len(out) == 1
    assert abs(out[0]["pnl"] - (40.0 - 0.12)) < ROUND


def test_vwap_pnl_fallback_short():
    trades = [fill(1, "SELL", 200, 1, T0, fee=0.0), fill(2, "BUY", 180, 1, T0 + 60_000, fee=0.0)]
    out = engine.reconstruct_walk("gate", "ETH_USDT", trades)
    assert out[0]["direction"] == "Short"
    assert abs(out[0]["pnl"] - 20.0) < ROUND


def test_funding_attributed_by_time():
    trades = [fill(1, "BUY", 100, 1, T0, pnl=5.0), fill(2, "SELL", 110, 1, T0 + 60_000, pnl=5.0),
              fill(3, "BUY", 200, 1, T0 + 120_000, pnl=5.0), fill(4, "SELL", 210, 1, T0 + 180_000, pnl=5.0)]
    # pos0 윈도 [T0, T0+60k], pos1 윈도 [T0+120k, T0+180k]
    events = [(T0 + 30_000, -1.0),    # pos0 안
              (T0 + 150_000, -0.5),   # pos1 안
              (T0 + 90_000, -0.2)]    # 두 포지션 사이(공백) → 마지막(pos1)
    out = engine.reconstruct_walk("binance", "BTCUSDT", trades, funding_events=events)
    assert len(out) == 2
    assert abs(out[0]["funding"] - (-1.0)) < ROUND
    assert abs(out[1]["funding"] - (-0.7)) < ROUND  # -0.5 + -0.2


def test_no_funding_events_zero():
    trades = [fill(1, "BUY", 100, 1, T0, pnl=5.0), fill(2, "SELL", 110, 1, T0 + 60_000, pnl=5.0)]
    out = engine.reconstruct_walk("binance", "BTCUSDT", trades)
    assert out[0].get("funding") == 0.0


def test_position_side_buckets():
    trades = [fill(1, "BUY", 100, 1, T0, pnl=10.0, pos_side="LONG"),
              fill(2, "SELL", 150, 1, T0 + 10_000, pnl=10.0, pos_side="LONG"),
              fill(3, "SELL", 300, 1, T0 + 5_000, pnl=10.0, pos_side="SHORT"),
              fill(4, "BUY", 250, 1, T0 + 20_000, pnl=10.0, pos_side="SHORT")]
    out = engine.reconstruct_walk("binance", "BTCUSDT", trades)
    assert len(out) == 2
    assert {r["direction"] for r in out} == {"Long", "Short"}


def test_idempotent_keys_stable():
    def mk():
        return [fill(1, "BUY", 100, 2, T0, pnl=0.0), fill(2, "SELL", 110, 2, T0 + 60_000, pnl=20.0)]
    a = engine.reconstruct_walk("binance", "BTCUSDT", mk())
    b = engine.reconstruct_walk("binance", "BTCUSDT", mk())
    assert [r["trade_id"] for r in a] == [r["trade_id"] for r in b]


def test_mae_mfe_prices_long():
    # ohlcv: [ts, open, high, low, close, vol] — 구간 [100, 300]
    oh = [[50, 0, 105, 95, 0, 0],      # 구간 밖(ts<100) → 제외
          [100, 0, 112, 98, 0, 0],
          [200, 0, 130, 93, 0, 0],     # 최고 130, 최저 93
          [400, 0, 999, 1, 0, 0]]      # 구간 밖(ts>300) → 제외
    mae, mfe = engine._mae_mfe_prices(oh, "Long", 100, 300)
    assert mae == 93 and mfe == 130  # 롱: mae=최저, mfe=최고


def test_mae_mfe_prices_short():
    oh = [[100, 0, 112, 98, 0, 0], [200, 0, 130, 93, 0, 0]]
    mae, mfe = engine._mae_mfe_prices(oh, "Short", 100, 300)
    assert mae == 130 and mfe == 93  # 숏: mae=최고(불리), mfe=최저(유리)


def test_mae_mfe_prices_no_candles_in_window():
    oh = [[50, 0, 105, 95, 0, 0], [400, 0, 110, 90, 0, 0]]
    assert engine._mae_mfe_prices(oh, "Long", 100, 300) == (None, None)


def test_realized_sum_invariant():
    trades = [fill(1, "BUY", 100, 1, T0, pnl=0.0), fill(2, "SELL", 110, 1, T0 + 1, pnl=10.0),
              fill(3, "BUY", 100, 1, T0 + 2, pnl=0.0), fill(4, "SELL", 95, 1, T0 + 3, pnl=-5.0)]
    out = engine.reconstruct_walk("binance", "BTCUSDT", trades)
    assert_pnl_sum(out, 5.0)


def test_binance_query_extends_before_lookback_for_mid_position_boundary():
    day = 86_400_000
    now = T0 + 20 * day
    all_fills = [
        fill(1, "BUY", 50_000, 1, now - 15 * day, pnl=0.0),
        fill(2, "SELL", 60_000, 1, now - 5 * day, pnl=10_000.0),
    ]

    class FakeExchange:
        def __init__(self):
            self.starts = []

        def milliseconds(self):
            return now

        def fapiPrivateGetUserTrades(self, params):
            if "fromId" in params:
                return [t for t in all_fills if int(t["id"]) >= int(params["fromId"])]
            start, end = int(params["startTime"]), int(params["endTime"])
            self.starts.append(start)
            return [t for t in all_fills if start <= int(t["time"]) <= end]

    ex = FakeExchange()
    rows = engine._binance_user_trades(ex, "BTCUSDT", lookback=10)
    assert [int(r["id"]) for r in rows] == [1, 2]
    assert min(ex.starts) < now - 10 * day  # 조회창 앞의 진입 체결까지 자동 보강
    rebuilt = engine.reconstruct_walk("binance", "BTCUSDT", rows)
    assert len(rebuilt) == 1 and rebuilt[0]["pnl"] == 10_000.0


def test_binance_realized_ledger_reconciliation_includes_open_partial_closes():
    fills = [
        {"time": 1_000, "realizedPnl": "0"},
        {"time": 2_000, "realizedPnl": "12.5"},
        {"time": 3_000, "realizedPnl": "-2.25"},
    ]
    audit = engine._reconcile_binance_realized("BTCUSDT", fills, 1_500, 10.25)
    assert audit == {"symbol": "BTCUSDT", "ledger": 10.25, "fills": 10.25, "delta": 0.0}


def test_binance_income_pages_without_dropping_same_tran_id_across_types():
    day = 86_400_000

    class FakeExchange:
        def __init__(self):
            self.pages = []

        def milliseconds(self):
            return 7 * day

        def fapiPrivateGetIncome(self, params):
            page = int(params["page"])
            self.pages.append(page)
            if page == 1:
                rows = [
                    {"incomeType": "COMMISSION", "tranId": 7,
                     "symbol": "ONDOUSDT", "income": "-0.1", "time": 1},
                    {"incomeType": "REALIZED_PNL", "tranId": 7,
                     "symbol": "ONDOUSDT", "income": "-82.29", "time": 1},
                ]
                rows.extend(
                    {"incomeType": "TRANSFER", "tranId": 100 + i,
                     "symbol": "", "income": "0", "time": 1}
                    for i in range(998)
                )
                return rows
            if page == 2:
                return [{"incomeType": "REALIZED_PNL", "tranId": 9999,
                         "symbol": "ONDOUSDT", "income": "-1", "time": 1}]
            raise AssertionError(f"unexpected page {page}")

    ex = FakeExchange()
    symbols, _, ledger = engine._binance_symbols_and_funding(ex, lookback=7, now=7 * day)
    assert ex.pages == [1, 2]
    assert "ONDOUSDT" in symbols
    assert ledger["ONDOUSDT"] == pytest.approx(-83.29)


def test_binance_realized_ledger_mismatch_stops_import():
    fills = [{"time": 2_000, "realizedPnl": "30"}]
    with pytest.raises(RuntimeError, match="기존 일지는 유지"):
        engine._reconcile_binance_realized("BTCUSDT", fills, 1_000, 12.0)


def test_binance_audit_uses_stable_utc_day_boundary_for_income_trade_time_skew():
    day = 86_400_000
    snapshot_end = 100 * day + 12_345
    exact_start = snapshot_end - 90 * day
    stable_start = engine._binance_window_start(snapshot_end, 90)
    fills = [{"time": exact_start - 500, "realizedPnl": "85.91"}]

    with pytest.raises(engine.RealizedPnlMismatch):
        engine._reconcile_binance_realized("BTCUSDT", fills, exact_start, 85.91, snapshot_end)

    audit = engine._reconcile_binance_realized("BTCUSDT", fills, stable_start, 85.91, snapshot_end)
    assert audit["delta"] == 0.0
    assert stable_start % day == 0
    assert stable_start <= exact_start < stable_start + day


def test_binance_audit_excludes_unsettled_fills_after_snapshot_end():
    fills = [
        {"time": 2_000, "realizedPnl": "10"},
        {"time": 3_001, "realizedPnl": "99"},
    ]
    audit = engine._reconcile_binance_realized("BTCUSDT", fills, 1_000, 10.0, 3_000)
    assert audit["fills"] == 10.0
    assert audit["window_end_ms"] == 3_000
