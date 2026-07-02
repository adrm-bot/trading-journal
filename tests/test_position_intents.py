"""사전 계획(보유 포지션) — 저장·매칭·소비·정직성(preplanned는 자동 경로 전용)."""
import os
from cryptography.fernet import Fernet

os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from app import db, engine  # noqa: E402


def _fresh(tmp_path):
    db.DB_PATH = str(tmp_path / "t.db")
    db.init()
    return db.upsert_user("a@b.c", "A")


def test_position_intent_roundtrip_overwrite_delete(tmp_path):
    uid = _fresh(tmp_path)
    db.set_position_intent(uid, "bybit", "BTCUSDT", "Long",
                           {"plan": "눌림 매수", "sl": 95.0, "tp": 120.0, "conviction": 4, "entry_snap": 100.0})
    ps = db.get_position_intents(uid)
    assert len(ps) == 1 and ps[0]["plan"] == "눌림 매수" and ps[0]["conviction"] == 4
    # 같은 키 재저장 = 덮어쓰기 (미지정 필드는 비움)
    db.set_position_intent(uid, "bybit", "BTCUSDT", "Long", {"plan": "수정", "sl": 96.0})
    ps = db.get_position_intents(uid)
    assert len(ps) == 1 and ps[0]["plan"] == "수정" and ps[0]["tp"] is None
    assert db.delete_position_intent(uid, "bybit", "BTCUSDT", "Long") is True
    assert db.get_position_intents(uid) == []


def test_apply_matches_earliest_close_and_consumes(tmp_path):
    uid = _fresh(tmp_path)
    db.set_position_intent(uid, "bybit", "BTCUSDT", "Long",
                           {"plan": "사전 계획", "strategy": "추세추종", "sl": 95.0})
    late = {"trade_id": "bybit:pos:BTCUSDT:2", "exchange": "bybit", "symbol": "BTCUSDT", "direction": "Long",
            "entry": 100, "exit": 111, "qty": 1, "pnl": 11, "closed_at": "2099-01-02 00:00:00",
            "status": "의도 미기입"}
    early = {"trade_id": "bybit:pos:BTCUSDT:1", "exchange": "bybit", "symbol": "BTCUSDT", "direction": "Long",
             "entry": 100, "exit": 110, "qty": 1, "pnl": 10, "closed_at": "2099-01-01 00:00:00",
             "status": "의도 미기입"}
    db.upsert_trade(uid, late)
    db.upsert_trade(uid, early)
    assert engine.apply_position_intents(uid, "bybit", [late, early]) == 1
    t1 = db.get_trade(uid, "bybit:pos:BTCUSDT:1")  # 가장 이른 청산에만 적용
    assert t1["plan"] == "사전 계획" and t1["strategy"] == "추세추종" and t1["sl"] == 95.0
    assert t1["status"] == "기록완료" and t1["preplanned"] == 1
    t2 = db.get_trade(uid, "bybit:pos:BTCUSDT:2")
    assert not t2["plan"] and not t2["preplanned"]
    assert db.get_position_intents(uid) == []  # 소비됨


def test_apply_skips_wrong_direction_and_past_close(tmp_path):
    uid = _fresh(tmp_path)
    # 방향 불일치 → 미적용·미소비
    db.set_position_intent(uid, "bybit", "BTCUSDT", "Short", {"plan": "숏 계획"})
    row = {"trade_id": "x", "exchange": "bybit", "symbol": "BTCUSDT", "direction": "Long",
           "entry": 100, "exit": 110, "qty": 1, "pnl": 10, "closed_at": "2099-01-01 00:00:00"}
    db.upsert_trade(uid, row)
    assert engine.apply_position_intents(uid, "bybit", [row]) == 0
    assert len(db.get_position_intents(uid)) == 1
    # 청산 시각이 계획 작성보다 과거(이미 닫힌 옛 거래) → 미적용
    db.set_position_intent(uid, "bybit", "ETHUSDT", "Long", {"plan": "늦은 계획"})
    old = {"trade_id": "y", "exchange": "bybit", "symbol": "ETHUSDT", "direction": "Long",
           "entry": 1, "exit": 2, "qty": 1, "pnl": 1, "closed_at": "2000-01-01 00:00:00"}
    db.upsert_trade(uid, old)
    assert engine.apply_position_intents(uid, "bybit", [old]) == 0
    t = db.get_trade(uid, "y")
    assert not t["plan"] and not t["preplanned"]


def test_preplanned_not_settable_via_update_intent(tmp_path):
    # preplanned는 _INTENT 밖 — /api/intent(사후 기입)로 못 세움 = 사전 기입 표식 세탁 불가
    uid = _fresh(tmp_path)
    db.upsert_trade(uid, {"trade_id": "z", "exchange": "bybit", "symbol": "B", "direction": "Long",
                          "entry": 1, "exit": 2, "qty": 1, "pnl": 1})
    db.update_intent(uid, "z", {"plan": "사후 기입", "preplanned": 1})
    t = db.get_trade(uid, "z")
    assert t["plan"] == "사후 기입"
    assert not t["preplanned"]
