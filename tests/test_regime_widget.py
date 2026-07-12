"""app.regime — 레짐 카드/레짐별 성과의 순수 로직 (네트워크·DB 불필요).

classify2는 research/regime의 검증된 2축 수식과 동일해야 하고(합성 시나리오 부호),
perf는 라벨 매칭·집계·미매칭 정직 카운트를 고정한다.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app import regime


def _bars(closes, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="15min", tz="UTC")
    c = pd.Series(closes, index=idx, dtype="float64")
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]),
                         "high": c * 1.001, "low": c * 0.999, "close": c})


def _walk(g, n, vol, drift=0.0, start=100.0):
    return start * np.cumprod(1.0 + drift + g.normal(0, vol, n))


def test_classify2_trend_and_squeeze():
    g = np.random.default_rng(5)
    ctx = _walk(g, 3200, 0.004)
    up = _walk(g, 400, 0.001, drift=0.003, start=ctx[-1])
    reg, conf, quad = regime.classify2(_bars(np.concatenate([ctx, up])), 15)
    assert quad is None  # oi 미제공 = 2축
    tail = reg.iloc[-60:]
    assert (tail == "TREND_UP").mean() > 0.8
    ok = conf[reg.notna()]
    assert ((ok >= 0) & (ok <= 1)).all()

    vols = 0.0006 * 0.99 ** np.arange(400)
    sq = ctx[-1] * np.cumprod(1.0 + g.normal(0, 1, 400) * vols)
    reg2, _, _ = regime.classify2(_bars(np.concatenate([ctx, sq])), 15)
    win = reg2.iloc[3200 + 60:3200 + 128]
    assert (win == "SQUEEZE").mean() > 0.8


def test_classify2_fuel_quadrant():
    """OI 제공 시: 상승+OI증가 구간에서 사분면 1(신규 롱), 라벨 투표로 TREND_UP 유지."""
    g = np.random.default_rng(7)
    ctx = _walk(g, 3200, 0.004)
    up = _walk(g, 400, 0.001, drift=0.003, start=ctx[-1])
    bars = _bars(np.concatenate([ctx, up]))
    # 5분 스냅샷 OI: 컨텍스트 평탄, 시나리오 구간 상승
    snap = pd.date_range(bars.index[0], bars.index[-1], freq="5min", tz="UTC")
    n_ctx5 = int(len(snap) * 3200 / 3600)
    rate = np.zeros(len(snap))
    rate[n_ctx5:] = 0.0008
    oi = pd.DataFrame({"snap_ts": snap,
                       "oi": 1e6 * np.cumprod(1 + rate + g.normal(0, 5e-5, len(snap)))})
    reg, conf, quad = regime.classify2(bars, 15, oi=oi)
    assert quad is not None
    tail_q = quad.iloc[-60:]
    assert (tail_q == 1).mean() > 0.7  # 신규 롱 유입
    assert (reg.iloc[-60:] == "TREND_UP").mean() > 0.8


def test_norm_sym():
    assert regime._norm_sym("BTC/USDT:USDT") == "BTCUSDT"
    assert regime._norm_sym("BTC_USDT") == "BTCUSDT"
    assert regime._norm_sym("enausdt") == "ENAUSDT"
    assert regime._norm_sym(None) is None


def test_perf_matches_entry_regime(monkeypatch):
    idx = pd.date_range("2025-01-01", periods=200, freq="15min", tz="UTC")
    lab = pd.Series(["RANGE"] * 100 + ["TREND_UP"] * 100, index=idx)
    monkeypatch.setattr(regime, "_labels_for", lambda sym, since: lab)

    trades = [
        # RANGE 구간 진입, 손실, R 있음
        {"symbol": "BTCUSDT", "opened_at": "2025-01-01T05:00:00", "pnl": -50.0, "r": -1.0},
        # TREND_UP 구간 진입(진입시각 우선), 이익
        {"symbol": "BTCUSDT", "opened_at": "2025-01-02T05:00:00",
         "closed_at": "2025-01-01T00:15:00", "pnl": 30.0, "r": 1.5},
        # 진입시각 없음 → 청산시각 폴백 (RANGE)
        {"symbol": "BTCUSDT", "closed_at": "2025-01-01T06:00:00", "pnl": 10.0},
        # 라벨 범위 밖(너무 미래) → 미매칭
        {"symbol": "BTCUSDT", "opened_at": "2025-03-01T00:00:00", "pnl": 5.0},
        # 심볼 없음 → 미매칭
        {"symbol": None, "opened_at": "2025-01-01T05:00:00", "pnl": 1.0},
    ]
    out = regime.perf(trades)
    by = {r["regime"]: r for r in out["rows"]}
    assert by["RANGE"]["n"] == 2 and by["RANGE"]["pnl"] == -40.0
    assert by["RANGE"]["avg_r"] == -1.0 and by["RANGE"]["n_r"] == 1
    assert by["TREND_UP"]["n"] == 1 and by["TREND_UP"]["win_rate"] == 1.0
    assert out["unmatched"] == 2
    assert "청산시각 사용 1건" in out["note"]


def test_perf_unknown_symbol_counts_unmatched(monkeypatch):
    monkeypatch.setattr(regime, "_labels_for", lambda sym, since: None)
    out = regime.perf([{"symbol": "NOPEUSDT", "opened_at": "2025-01-01T00:00:00", "pnl": 1.0}])
    assert out["rows"] == [] and out["unmatched"] == 1


def test_build_positioning_tracks_oi_crowd_and_taker_flow():
    idx = pd.date_range("2026-06-01", periods=12 * 24 * 8, freq="5min", tz="UTC")
    oi = pd.DataFrame({"snap_ts": idx,
                       "oi": np.linspace(1_000_000, 1_080_000, len(idx)),
                       "oi_usd": np.linspace(30e9, 33e9, len(idx))})
    ratio_rows = []
    taker_rows = []
    for i, ts in enumerate(idx[-500:]):
        long = 0.52 + i / 499 * 0.13
        ratio_rows.append({"timestamp": int(ts.timestamp() * 1000),
                           "longAccount": str(long), "shortAccount": str(1 - long),
                           "longShortRatio": str(long / (1 - long))})
        taker_rows.append({"timestamp": int(ts.timestamp() * 1000),
                           "buyVol": "60", "sellVol": "40"})

    week_idx = pd.date_range("2026-06-01", periods=24 * 8, freq="1h", tz="UTC")
    week_rows = []
    for i, ts in enumerate(week_idx):
        long = 0.50 + i / (len(week_idx) - 1) * 0.15
        week_rows.append({"timestamp": int(ts.timestamp() * 1000),
                          "longAccount": str(long), "shortAccount": str(1 - long),
                          "longShortRatio": str(long / (1 - long))})

    out = regime.build_positioning(
        oi, ratio_rows, taker_rows,
        [{"sym": "BTC", "usd": 33e9, "chg24": 1.2}], "BTCUSDT", week_rows)

    assert out["available"] is True and out["oi_available"] is True
    assert out["total_usd"] == 33e9
    assert out["tf"]["24h"] is not None and out["tf"]["7d"] is not None
    assert out["long_pct"] == 65.0 and out["short_pct"] == 35.0
    assert out["long_delta_pp"]["1h"] > 0 and out["crowd_trend"] == "long_increasing"
    assert out["long_delta_pp"]["7d"] > 0
    assert out["ratio_asof"].startswith("2026-")
    assert out["taker_buy_pct_1h"] == 60.0 and out["taker_sell_pct_1h"] == 40.0


def test_build_positioning_honestly_degrades_when_all_sources_missing():
    out = regime.build_positioning(None, [], [], [], "BTCUSDT")
    assert out["available"] is False
    assert out["oi_available"] is False
    assert out["tf"] == [] or out["tf"] == {}
