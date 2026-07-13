"""market._alt_board — 알트 상대강도 보드의 저항성.
신규·미상장 심볼(HYPE 등)을 늘려도, 거래소에 없는 심볼 하나가 전체 보드를 죽이지 않아야 한다.
실 ccxt/네트워크는 쓰지 않고 가짜 거래소로 형태·저항성만 고정한다."""
from app import market


def _candles(n=300, base=100.0, step=0.5):
    v = [base + i * step for i in range(n)]
    return [[i, v[i], v[i], v[i], v[i], 1.0] for i in range(n)]  # closes만 쓰이므로 o=h=l=c


class _FakeEx:
    """fetch_ohlcv만 노출. bad에 든 베이스 심볼은 BadSymbol처럼 예외."""
    def __init__(self, bad=()):
        self.bad = set(bad)
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=300):
        self.calls.append(symbol)
        base = symbol.split("/")[0]
        if base in self.bad:
            raise ValueError("BadSymbol: " + symbol)
        return _candles()


def test_alt_board_skips_missing_symbol_keeps_rest():
    ex = _FakeEx(bad={"HYPE"})  # HYPE가 이 거래소 현물에 없다고 가정
    eth, board = market._alt_board(ex, {"chg7": 5.0})
    syms = {b["sym"] for b in board}
    assert "HYPE" not in syms                    # 미상장 심볼은 조용히 제외
    assert {"ETH", "ONDO", "ENA", "RENDER"} <= syms  # 정상 심볼은 유지
    assert eth is not None                        # ETH 대장 스냅샷 확보
    vs = [b["vs_btc_7d"] for b in board]
    assert vs == sorted(vs, reverse=True)         # vs_btc_7d 내림차순 정렬


def test_alt_board_all_missing_returns_empty_not_crash():
    ex = _FakeEx(bad=set(market.ALTS))
    eth, board = market._alt_board(ex, {"chg7": 1.0})
    assert board == [] and eth is None            # 전부 실패해도 예외 없이 빈 보드


def test_alt_board_no_btc_chg7_yields_no_rows():
    # BTC 7일 수익률을 모르면 상대수익률을 만들 수 없음 → 행 없음(추정치 날조 금지)
    eth, board = market._alt_board(_FakeEx(), {"chg7": None})
    assert board == []


def _series_from_returns(returns, start=100.0):
    values = [start]
    for r in returns:
        values.append(values[-1] * (1 + r))
    return [[i, v] for i, v in enumerate(values)]


def test_capture_profile_rewards_upside_participation_and_downside_defense():
    # BTC +1% 날 알트 +1.5%, BTC -1% 날 알트 -0.5% → 상방 150, 하방 50, 강도 +100.
    btc_returns = [0.01, -0.01] * 30
    alt_returns = [0.015, -0.005] * 30
    p = market._capture_profile(_series_from_returns(alt_returns), _series_from_returns(btc_returns))
    assert p["up_capture_60d"] == 150.0
    assert p["down_capture_60d"] == 50.0
    assert p["rs_score"] == 100.0
    assert p["sample_up"] == 30 and p["sample_down"] == 30


def test_capture_profile_requires_both_up_and_down_samples():
    only_up = [0.01] * 10
    assert market._capture_profile(_series_from_returns(only_up), _series_from_returns(only_up)) is None


def test_coin_includes_price_spark_newest_first():
    # 가격 추이 스파크: 이미 받은 일봉 재사용, newest-first(F&G·알트시즌 규약), 30개
    c = market._coin(_FakeEx(), "BTC/USDT")
    assert len(c["spark"]) == 30
    assert c["spark"][0] == c["price"]           # 첫 원소 = 최신 종가
    assert c["spark"][0] > c["spark"][-1]        # 오름세 캔들이므로 최신 > 과거


def test_coin_mtf_includes_horizon_matched_price_changes_without_extra_series():
    c = market._coin(_FakeEx(), "BTC/USDT", mtf=True)
    assert set(c["chg_tf"]) == {"1h", "4h", "24h", "7d"}
    assert c["chg_tf"]["1h"] < c["chg_tf"]["4h"] < c["chg_tf"]["24h"] < c["chg_tf"]["7d"]


def test_alts_are_unique_and_include_requested_sectors():
    assert len(market.ALTS) == len(set(market.ALTS))       # 중복 없음
    assert market.ALTS[0] == "ETH"                          # 대장 스냅샷용 첫 항목
    assert len(market.ALTS) >= 40                           # 대형 시총 + 섹터 대표 확장
    for s in ("HYPE", "ONDO", "ENA", "RENDER", "FET", "WLD",
              "TRX", "TON", "XLM", "HBAR", "MNT", "PENDLE", "PYTH", "IMX", "DYDX"):
        assert s in market.ALTS
