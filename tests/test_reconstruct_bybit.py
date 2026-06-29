"""reconstruct_bybit(closed-pnl 그룹핑) 골든 케이스."""
import pytest
from app import engine
from conftest import cpnl, ts

C0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _fix_gap(monkeypatch):
    # 그룹핑 경계 고정(환경변수 POSITION_GAP_HOURS 영향 차단)
    monkeypatch.setattr(engine, "POS_GAP_HOURS", 12.0)


def test_single_long():
    rows = [cpnl("o1", "BTCUSDT", "Sell", 100, 110, 2, 20.0, C0, open_fee=0.1, close_fee=0.1)]
    out = engine.reconstruct_bybit(rows)
    assert len(out) == 1
    r = out[0]
    assert r["direction"] == "Long"
    assert r["entry"] == 100.0 and r["exit"] == 110.0 and r["qty"] == 2.0
    assert r["pnl"] == 20.0
    assert abs(r["fees"] - 0.2) < 1e-9
    assert r["trade_id"] == "bybit:pos:BTCUSDT:o1"
    assert r["opened_at"] == ts(C0) and r["closed_at"] == ts(C0)
    assert r["liquidated"] == 0


def test_single_short():
    rows = [cpnl("o9", "ETHUSDT", "Buy", 200, 180, 1, 20.0, C0)]
    out = engine.reconstruct_bybit(rows)
    assert out[0]["direction"] == "Short"
    assert out[0]["entry"] == 200.0 and out[0]["exit"] == 180.0


def test_group_within_gap():
    rows = [cpnl("a", "BTCUSDT", "Sell", 100, 110, 1, 10.0, C0, updated=C0),
            cpnl("b", "BTCUSDT", "Sell", 100, 120, 1, 20.0, C0 + 3600_000, updated=C0 + 3600_000)]
    out = engine.reconstruct_bybit(rows)
    assert len(out) == 1
    r = out[0]
    assert r["qty"] == 2.0
    assert r["entry"] == 100.0
    assert r["exit"] == 115.0
    assert r["pnl"] == 30.0
    assert r["fill_count"] == 2
    assert r["trade_id"] == "bybit:pos:BTCUSDT:b"


def test_split_beyond_gap():
    rows = [cpnl("a", "BTCUSDT", "Sell", 100, 110, 1, 10.0, C0, updated=C0),
            cpnl("b", "BTCUSDT", "Sell", 100, 120, 1, 20.0, C0 + 13 * 3600_000, updated=C0 + 13 * 3600_000)]
    out = engine.reconstruct_bybit(rows)
    assert len(out) == 2
    assert {r["trade_id"] for r in out} == {"bybit:pos:BTCUSDT:a", "bybit:pos:BTCUSDT:b"}


def test_direction_and_symbol_boundaries():
    rows = [cpnl("a", "BTCUSDT", "Sell", 100, 110, 1, 10.0, C0),
            cpnl("b", "BTCUSDT", "Buy", 110, 100, 1, 10.0, C0 + 60_000),
            cpnl("c", "ETHUSDT", "Sell", 100, 110, 1, 10.0, C0 + 120_000)]
    out = engine.reconstruct_bybit(rows)
    assert len(out) == 3


@pytest.mark.parametrize("etype", ["Bust", "bustTrade", "AdlTrade", "adltrade"])
def test_liquidation_flag(etype):
    rows = [cpnl("a", "BTCUSDT", "Sell", 100, 90, 1, -10.0, C0, exec_type=etype)]
    out = engine.reconstruct_bybit(rows)
    assert out[0]["liquidated"] == 1


def test_idempotent_key_is_last_close():
    rows = [cpnl("early", "BTCUSDT", "Sell", 100, 110, 1, 10.0, C0, updated=C0),
            cpnl("late", "BTCUSDT", "Sell", 100, 120, 1, 20.0, C0 + 60_000, updated=C0 + 60_000)]
    out = engine.reconstruct_bybit(rows)
    assert out[0]["trade_id"] == "bybit:pos:BTCUSDT:late"


def test_zero_size_group_skipped():
    rows = [cpnl("z", "BTCUSDT", "Sell", 0, 0, 0, 0.0, C0)]
    rows[0]["qty"] = 0
    assert engine.reconstruct_bybit(rows) == []


def test_exit_reason_unknown_without_execmap():
    rows = [cpnl("o1", "BTCUSDT", "Sell", 100, 110, 1, 5.0, C0)]
    assert engine.reconstruct_bybit(rows)[0]["exit_reason"] == "unknown"


def test_exit_reason_sl_tp_manual():
    base = cpnl("oX", "BTCUSDT", "Sell", 100, 95, 1, -5.0, C0)
    assert engine.reconstruct_bybit([dict(base)], {"oX": "StopLoss"})[0]["exit_reason"] == "sl_hit"
    assert engine.reconstruct_bybit([dict(base)], {"oX": "TakeProfit"})[0]["exit_reason"] == "tp_hit"
    assert engine.reconstruct_bybit([dict(base)], {"oX": ""})[0]["exit_reason"] == "manual"


def test_exit_reason_liquidation_priority():
    base = cpnl("oL", "BTCUSDT", "Sell", 100, 90, 1, -10.0, C0, exec_type="BustTrade")
    # 강제청산이면 stopOrderType이 있어도 liquidation 우선
    assert engine.reconstruct_bybit([dict(base)], {"oL": "StopLoss"})[0]["exit_reason"] == "liquidation"


def test_order_independent():
    base = [cpnl("a", "BTCUSDT", "Sell", 100, 110, 1, 10.0, C0, updated=C0),
            cpnl("b", "BTCUSDT", "Sell", 100, 120, 1, 20.0, C0 + 3600_000, updated=C0 + 3600_000)]
    out1 = engine.reconstruct_bybit([dict(base[0]), dict(base[1])])
    out2 = engine.reconstruct_bybit([dict(base[1]), dict(base[0])])
    assert [r["trade_id"] for r in out1] == [r["trade_id"] for r in out2]
    assert out1[0]["exit"] == out2[0]["exit"]
