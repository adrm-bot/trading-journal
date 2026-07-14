"""app.regime — 레짐 카드/레짐별 성과의 순수 로직 (네트워크·DB 불필요).

운영 경로는 canonical REGIME v2.1과 동일해야 한다. classify2는 v1 연구 회귀 호환만
검증하고, perf는 라벨 매칭·집계·미매칭 정직 카운트를 고정한다.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app import regime
from research.regime import classifier as research_classifier
from research.regime import features as research_features


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


def test_production_v21_matches_canonical_price_only_core():
    g = np.random.default_rng(17)
    bars = _bars(_walk(g, 4200, 0.003, drift=0.00015))
    got_reg, got_conf = regime._classify_v2_bars(bars)
    feat = research_features.compute_v2(bars, research_features.v2_params_for_chart(15))
    expected = research_classifier.classify_v2(feat)
    pd.testing.assert_series_equal(got_reg, expected["regime"], check_names=False)
    pd.testing.assert_series_equal(got_conf, expected["confidence"], check_names=False)


def test_live_v21_exposes_one_core_and_context_only(monkeypatch):
    g = np.random.default_rng(19)
    bars = _bars(_walk(g, 6000, 0.003, drift=0.0001))
    monkeypatch.setattr(regime, "_fetch_klines", lambda *args, **kwargs: bars)
    monkeypatch.setattr(regime, "_cached_oi", lambda *args, **kwargs: None)
    regime._live_cache.clear()
    out = regime.live("BTCUSDT")
    assert out["available"] is True and out["version"] == "2.1"
    assert len(out["tfs"]) == 1 and out["tfs"][0]["tf"] == "15m"
    assert out["quad"] is None and out["oi_separate"] is True
    assert out["oi_context_quad"] is None
    assert out["core_minutes_by_chart"] == {5: 15, 15: 15, 60: 15, 240: 15}
    assert {c["tf"] for c in out["contexts"]} == {"1h", "4h"}


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
    assert "청산 시각 대체 1건" in out["note"]


def test_oi_context_quadrant_is_separate_from_v21_core():
    g = np.random.default_rng(23)
    bars = _bars(_walk(g, 3600, 0.002, drift=0.00025))
    snap = pd.date_range(bars.index[0], bars.index[-1], freq="5min", tz="UTC")
    oi = pd.DataFrame({"snap_ts": snap,
                       "oi": 1_000_000 * np.cumprod(1 + g.normal(0.00008, 0.00003, len(snap)))})
    out = regime._oi_context_quad(bars, oi)
    assert out is not None
    assert out["separate_from_regime"] is True
    assert out["lookback_hours"] == 6 and out["change_window_hours"] == 2
    assert out["plot"]["points"]


def test_oi_context_quadrants_use_actionable_position_flow_labels():
    assert regime.QUAD_TEXT[1].startswith("신규 롱")
    assert regime.QUAD_TEXT[2].startswith("신규 숏")
    assert regime.QUAD_TEXT[3].startswith("숏 커버")
    assert regime.QUAD_TEXT[4].startswith("롱 청산")


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
    assert out["oi_risk"]["level"] == "risk"
    assert out["oi_risk"]["vulnerable_side"] == "long"
    assert out["oi_risk"]["quantity_only"] is True
    assert out["oi_risk"]["active_count"] == 1
    assert out["oi_risk"]["aux_active_count"] >= 2
    assert out["oi_risk"]["label"] == "매우 높은 구간"
    assert out["oi_risk"]["sample_n"] == len(idx)
    assert out["oi_risk"]["sample_days"] > 7
    assert out["oi_risk"]["sample_interval_minutes"] == 5
    assert out["oi_risk"]["threshold_kind"] == "descriptive_quantile"
    assert out["oi_risk"]["thresholds_validated"] is False


def test_build_positioning_honestly_degrades_when_all_sources_missing():
    out = regime.build_positioning(None, [], [], [], "BTCUSDT")
    assert out["available"] is False
    assert out["oi_available"] is False
    assert out["tf"] == [] or out["tf"] == {}
    assert out["oi_risk"]["level"] == "unknown"


def test_oi_quantity_stage_uses_only_absolute_amount_percentile():
    # 강한 증가·계정 쏠림·테이커 쏠림도 낮은 절대량 단계를 승급시키지 않는다.
    safe = regime.classify_oi_risk(
        48, {"1h": 3.0, "4h": 7.0, "24h": 15.0}, 72, 28, 68, 32)
    assert safe["level"] == "safe" and safe["active_count"] == 0
    assert safe["aux_active_count"] == 3

    watch = regime.classify_oi_risk(
        88, {"1h": 0.1, "4h": 0.4, "24h": 1.2}, 52, 48, 53, 47)
    assert watch["level"] == "watch" and watch["active_count"] == 0

    # 반대로 흐름이 조용해도 절대량이 90백분위 이상이면 과열 단계다.
    danger = regime.classify_oi_risk(
        92, {"1h": 0.1, "4h": 0.4, "24h": 1.2}, 52, 48, 53, 47)
    assert danger["level"] == "risk" and danger["active_count"] == 1
    assert danger["quantity_percentile"] == 92
    assert danger["thresholds"] == {"watch": 70, "risk": 90}
    assert danger["quantity_only"] is True
    assert danger["label"] == "매우 높은 구간"
    assert danger["exceedance_pct"] == 8
    assert danger["thresholds_validated"] is False
