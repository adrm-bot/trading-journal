"""liquidations — 순수 파싱·집계만 고정(라이브 WS/네트워크는 검증 대상, 여기선 미사용).
롱/숏 매핑(거래소 필드 의미)과 가격대 버킷·윈도우 필터의 정직성을 못박는다."""
from app import liquidations as liq


def test_parse_bybit_side_mapping():
    # Bybit: 청산 주문 Sell → 롱 포지션 청산 / Buy → 숏 청산
    msg = {"topic": "allLiquidation.BTCUSDT", "ts": 1000,
           "data": [{"T": 1000, "s": "BTCUSDT", "S": "Sell", "v": "0.5", "p": "45000"},
                    {"T": 1001, "s": "BTCUSDT", "S": "Buy", "v": "0.2", "p": "45010"}]}
    out = liq.parse_bybit(msg)
    assert out[0][1] == "long" and out[1][1] == "short"
    assert out[0][0] == "BTCUSDT" and out[0][2] == 45000.0 and out[0][3] == 0.5


def test_parse_binance_side_mapping():
    # Binance forceOrder: SELL → 롱 청산, BUY → 숏 청산. avgPrice(ap) 우선.
    m_sell = {"data": {"o": {"s": "ETHUSDT", "S": "SELL", "q": "3", "ap": "2400", "T": 5}}}
    m_buy = {"o": {"s": "ETHUSDT", "S": "BUY", "q": "1", "p": "2410", "T": 6}}
    assert liq.parse_binance(m_sell)[0][1] == "long"
    assert liq.parse_binance(m_buy)[0][1] == "short"
    assert liq.parse_binance(m_sell)[0][2] == 2400.0  # ap 사용


def _ev(t, sym, side, price, qty):
    return {"t": t, "sym": sym, "side": side, "price": price, "qty": qty,
            "notional": round(price * qty, 2)}


def test_aggregate_window_and_totals():
    now = 10_000_000
    events = [
        _ev(now - 5 * 60_000, "BTCUSDT", "long", 45000, 1),      # 창 안(-5분)
        _ev(now - 59 * 60_000, "BTCUSDT", "short", 45500, 2),    # 창 안(-59분)
        _ev(now - 120 * 60_000, "BTCUSDT", "long", 44000, 5),    # 창 밖(-120분) → 제외
    ]
    a = liq._aggregate(events, now, window_min=60)
    assert a["n"] == 2
    assert a["longs_usd"] == 45000.0
    assert a["shorts_usd"] == 91000.0
    assert {t["sym"] for t in a["tape"]} == {"BTCUSDT"}


def test_aggregate_size_bands_are_fixed_and_preserve_totals():
    now = 10_000_000
    events = [
        _ev(now, "BTCUSDT", "long", 50_000, 1),       # 소형 $50k
        _ev(now, "BTCUSDT", "short", 50_000, 4),      # 중형 $200k
        _ev(now, "BTCUSDT", "long", 50_000, 30),      # 대형 $1.5m
    ]
    out = liq._aggregate(events, now)
    bands = {b["key"]: b for b in out["size_bands"]}
    assert bands["small"]["n"] == 1 and bands["small"]["usd"] == 50_000
    assert bands["medium"]["n"] == 1 and bands["medium"]["short_usd"] == 200_000
    assert bands["large"]["n"] == 1 and bands["large"]["long_usd"] == 1_500_000
    assert sum(b["usd"] for b in bands.values()) == out["longs_usd"] + out["shorts_usd"]


def test_aggregate_price_buckets_split_sides():
    now = 1_000_000
    ev = [_ev(now, "BTCUSDT", "long", 44000 + i * 100, 1) for i in range(4)]
    ev += [_ev(now, "BTCUSDT", "short", 44050 + i * 100, 1) for i in range(4)]
    a = liq._aggregate(ev, now, window_min=60, nbins=8)
    binmap = a["map"]
    assert binmap["symbol"] == "BTCUSDT"
    # 롱·숏 노셔널이 버킷에 분리 집계됐는지(합이 전체와 일치)
    tot_long = sum(b["long"] for b in binmap["bins"])
    tot_short = sum(b["short"] for b in binmap["bins"])
    assert round(tot_long, 2) == round(a["longs_usd"], 2)
    assert round(tot_short, 2) == round(a["shorts_usd"], 2)


def test_aggregate_map_symbol_only():
    now = 1_000_000
    ev = [_ev(now, "BTCUSDT", "long", 45000, 1), _ev(now, "ETHUSDT", "long", 2400, 10)]
    a = liq._aggregate(ev, now, window_min=60, map_symbol="BTCUSDT")
    # 맵은 map_symbol만 — ETH 노셔널은 총계엔 들어가도 BTC 맵 버킷엔 없음
    assert a["longs_usd"] == 45000.0 + 24000.0
    assert a["map"]["price"] == 45000.0


def test_snapshot_not_started_returns_unavailable():
    assert liq.snapshot().get("available") is False
