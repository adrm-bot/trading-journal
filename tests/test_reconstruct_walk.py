"""reconstruct_walk(Binance/Gate 공용) + _finalize_pos 골든 케이스."""
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
