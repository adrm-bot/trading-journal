"""재적재(창 교체) — 겹침 중복 누적 차단 + 정확 복원. 거래소 어댑터는 스텁으로 대체."""
import os
from cryptography.fernet import Fernet

os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from app import db, engine  # noqa: E402


def _fresh(tmp_path):
    db.DB_PATH = str(tmp_path / "t.db")
    db.init()
    return db.upsert_user("a@b.c", "A")


def _pos(tid, oa, ca, pnl, **kw):
    base = {"trade_id": tid, "exchange": "bybit", "symbol": "BTCUSDT", "direction": "Short",
            "entry": 65000.0, "exit": 61000.0, "qty": 3.0, "pnl": pnl,
            "opened_at": oa, "closed_at": ca, "status": "의도 미기입"}
    base.update(kw)
    return base


def test_delete_auto_trades_scopes(tmp_path):
    uid = _fresh(tmp_path)
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:1", "2026-06-03 07:00:00", "2026-06-05 13:00:00", 100))
    db.upsert_trade(uid, _pos("binance:pos:ETHUSDT:9", "2026-06-03 07:00:00", "2026-06-05 13:00:00", 50, exchange="binance", symbol="ETHUSDT"))
    # bybit 자동행만 삭제
    assert db.delete_auto_trades(uid, "bybit") == 1
    assert {t["exchange"] for t in db.get_trades(uid)} == {"binance"}


def test_delete_unannotated_only_preserves_notes(tmp_path):
    uid = _fresh(tmp_path)
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:1", "2026-06-03 07:00:00", "2026-06-05 13:00:00", 100))
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:2", "2026-06-06 07:00:00", "2026-06-07 13:00:00", 200))
    db.update_intent(uid, "bybit:pos:BTCUSDT:2", {"plan": "내 복기", "status": "기록완료"})
    removed = db.delete_auto_trades(uid, "bybit", unannotated_only=True)
    assert removed == 1  # 주석 없는 것만
    rows = db.get_trades(uid)
    assert len(rows) == 1 and rows[0]["plan"] == "내 복기"


def test_resync_replaces_overlap_duplicates(tmp_path, monkeypatch):
    uid = _fresh(tmp_path)
    db.set_connection(uid, "bybit", {"key": "k" * 10, "secret": "s" * 20})
    # 과거 버그 상태: 같은 숏이 겹치는 두 조각으로 이미 쌓임(이중계상) — pnl 합 46k
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:A", "2026-06-03 07:29:42", "2026-06-05 13:52:30", 31493.46))
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:B", "2026-06-04 04:20:39", "2026-06-05 13:51:33", 14476.29))
    assert round(sum(t["pnl"] for t in db.get_trades(uid)), 2) == 45969.75

    # 거래소 원장(정답) = 하나의 숏 한 건만. 어댑터 스텁으로 교체.
    truth = [_pos("bybit:pos:BTCUSDT:A", "2026-06-03 07:29:42", "2026-06-05 13:52:30", 22000.0)]
    monkeypatch.setitem(engine.ADAPTERS, "bybit", lambda cred, lb: truth)

    res = engine.resync_user(uid)
    rows = db.get_trades(uid)
    assert len(rows) == 1                                   # 겹침 제거
    assert rows[0]["pnl"] == 22000.0                        # 원장 기준 정확 복원
    assert res["bybit"]["error"] is None


def test_routine_pull_does_not_accumulate(tmp_path, monkeypatch):
    uid = _fresh(tmp_path)
    db.set_connection(uid, "bybit", {"key": "k" * 10, "secret": "s" * 20})
    # 재풀링마다 trade_id/경계가 흔들려도(같은 포지션이 다른 조각으로) 창 교체라 누적 안 됨
    v1 = [_pos("bybit:pos:BTCUSDT:A", "2026-06-03 07:29:42", "2026-06-05 13:52:30", 22000.0)]
    monkeypatch.setitem(engine.ADAPTERS, "bybit", lambda cred, lb: v1)
    engine.pull_user(uid, lookback=120)
    v2 = [_pos("bybit:pos:BTCUSDT:B", "2026-06-03 07:30:10", "2026-06-05 13:52:30", 22000.0)]  # 경계 흔들림
    monkeypatch.setitem(engine.ADAPTERS, "bybit", lambda cred, lb: v2)
    engine.pull_user(uid, lookback=120)
    rows = db.get_trades(uid)
    assert len(rows) == 1 and rows[0]["pnl"] == 22000.0     # 두 번 풀해도 1건(누적 X)


def test_resync_keeps_out_of_window_history(tmp_path, monkeypatch):
    uid = _fresh(tmp_path)
    db.set_connection(uid, "bybit", {"key": "k" * 10, "secret": "s" * 20})
    # 창(120일)보다 훨씬 오래된 자동행은 재조회 불가 → 보존해야(삭제 금지)
    old = _pos("bybit:pos:BTCUSDT:OLD", "2020-01-01 00:00:00", "2020-01-02 00:00:00", 999.0)
    db.upsert_trade(uid, old)
    monkeypatch.setitem(engine.ADAPTERS, "bybit", lambda cred, lb: [])  # 최근 창엔 아무것도 없음
    engine.resync_user(uid)
    tids = {t["trade_id"] for t in db.get_trades(uid)}
    assert "bybit:pos:BTCUSDT:OLD" in tids                  # 오래된 기록 보존
