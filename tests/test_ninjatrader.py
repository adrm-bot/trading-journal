"""NinjaTrader(Tradovate) 어댑터 — 순수 함수(정규화·포인트가치 스케일)와 walk 왕복.
네트워크 의존 경로(_nt_http/_nt_auth)는 실계정 검증 대상 — 여기선 형태 변환의 정직성만 고정한다."""
from datetime import datetime, timezone

from app import engine

DAY = 86_400_000


def _ms(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _fill(fid, ts_iso, action, price, qty, contract_id=101):
    return {"id": fid, "timestamp": ts_iso, "action": action, "price": price,
            "qty": qty, "contractId": contract_id}


def test_nt_ts_ms_parses_iso_z():
    assert engine._nt_ts_ms("2026-07-01T00:00:00Z") == _ms("2026-07-01T00:00:00")
    assert engine._nt_ts_ms("2026-07-01T00:00:00.500Z") == _ms("2026-07-01T00:00:00") + 500
    assert engine._nt_ts_ms(None) is None
    assert engine._nt_ts_ms("not-a-date") is None


def test_nt_normalize_filters_and_groups():
    start = engine._nt_ts_ms("2026-07-01T00:00:00Z")
    fills = [
        _fill(1, "2026-07-01T01:00:00Z", "Buy", 5000.0, 2),
        _fill(2, "2026-07-01T02:00:00Z", "Sell", 5010.0, 2),
        _fill(3, "2026-06-01T00:00:00Z", "Buy", 4000.0, 1),          # lookback 이전 → 제외
        _fill(4, "2026-07-01T03:00:00Z", "Expired", 5000.0, 1),      # 액션 불량 → 제외
        _fill(5, "2026-07-01T03:00:00Z", "Buy", None, 1),            # 가격 없음 → 제외
        _fill(6, "2026-07-01T04:00:00Z", "Sell", 2350.0, 1, 202),    # 다른 계약 → 별도 그룹
        {"id": 7, "timestamp": "2026-07-01T05:00:00Z", "action": "Buy", "price": 1.0, "qty": 1},  # contractId 없음
    ]
    by = engine._nt_normalize_fills(fills, {1: 2.5}, start)
    assert set(by.keys()) == {101, 202}
    assert [f["id"] for f in by[101]] == [1, 2]
    f1 = by[101][0]
    assert f1["side"] == "BUY" and f1["commission"] == 2.5 and f1["realizedPnl"] == 0.0
    assert by[101][1]["commission"] == 0.0  # 수수료 조회 실패분은 0


def test_nt_walk_roundtrip_scales_by_value_per_point():
    # MES 롱 2계약: 5000 진입 → 5010 청산, 포인트가치 $5, 수수료 왕복 $2.5+$2.5
    start = engine._nt_ts_ms("2026-07-01T00:00:00Z")
    fills = [_fill(1, "2026-07-01T01:00:00Z", "Buy", 5000.0, 2),
             _fill(2, "2026-07-01T02:00:00Z", "Sell", 5010.0, 2)]
    by = engine._nt_normalize_fills(fills, {1: 2.5, 2: 2.5}, start)
    rows = engine._nt_scale_pnl(engine.reconstruct_walk("ninjatrader", "MESU6", by[101]), 5.0)
    assert len(rows) == 1
    r = rows[0]
    assert r["direction"] == "Long" and r["entry"] == 5000.0 and r["exit"] == 5010.0
    assert r["pnl"] == 10 * 2 * 5.0 - 5.0  # (가격차 × 수량 × vpp) − 수수료 = 95
    assert r["trade_id"].startswith("ninjatrader:pos:MESU6:")


def test_nt_scale_pnl_short():
    rows = [{"direction": "Short", "entry": 2400.0, "exit": 2380.0, "qty": 3, "pnl": 0.0, "fees": 6.0}]
    out = engine._nt_scale_pnl(rows, 20.0)  # NQ류 $20/pt
    assert out[0]["pnl"] == (2400.0 - 2380.0) * 3 * 20.0 - 6.0  # 1194.0


def test_nt_adapters_registered():
    assert "ninjatrader" in engine.ADAPTERS
