"""bulk_fill_unplanned 정직성 + delete_user 카스케이드."""
import os
from cryptography.fernet import Fernet

os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())  # set_connection 암호화용 임시 키

from app import db  # noqa: E402


def _fresh(tmp_path):
    db.DB_PATH = str(tmp_path / "t.db")  # 모듈 전역 교체 (conn()이 호출 시점에 읽음)
    db.init()
    return db.upsert_user("a@b.c", "A")


def test_bulk_fill_ignores_strategy_and_setup(tmp_path):
    uid = _fresh(tmp_path)
    db.upsert_trade(uid, {"trade_id": "x1", "exchange": "bybit", "symbol": "BTCUSDT",
                          "direction": "Long", "entry": 100, "exit": 110, "qty": 1, "pnl": 10,
                          "status": "의도 미기입"})
    n = db.bulk_fill_unplanned(uid, {"plan": "사후 일괄 정리", "strategy": "추세추종",
                                     "setup": "돌파", "emotion": "차분"})
    assert n == 1
    r = db.get_trades(uid)[0]
    assert r["status"] == "기록완료"
    assert r["plan"] == "사후 일괄 정리"
    assert r["emotion"] == "차분"
    # 전략·셋업은 절대 일괄로 박히면 안 됨(통계 세탁 차단)
    assert not r["strategy"]
    assert not r["setup"]


def test_bulk_fill_only_touches_unplanned(tmp_path):
    uid = _fresh(tmp_path)
    db.upsert_trade(uid, {"trade_id": "done1", "exchange": "bybit", "symbol": "ETHUSDT",
                          "direction": "Long", "entry": 1, "exit": 2, "qty": 1, "pnl": 1,
                          "status": "기록완료"})
    db.update_intent(uid, "done1", {"strategy": "눌림목"})  # 전략은 의도전용 — update_intent로만 설정
    db.upsert_trade(uid, {"trade_id": "pend1", "exchange": "bybit", "symbol": "SOLUSDT",
                          "direction": "Long", "entry": 1, "exit": 2, "qty": 1, "pnl": 1,
                          "status": "의도 미기입"})
    n = db.bulk_fill_unplanned(uid, {"plan": "사후 정리"})
    assert n == 1  # 미기입 1건만
    rows = {r["trade_id"]: r for r in db.get_trades(uid)}
    assert rows["done1"]["strategy"] == "눌림목"  # 기존 기록완료 거래는 건드리지 않음
    assert rows["pend1"]["status"] == "기록완료"


def test_delete_user_cascades(tmp_path):
    uid = _fresh(tmp_path)
    db.set_connection(uid, "bybit", {"key": "k" * 10, "secret": "s" * 20})
    db.upsert_trade(uid, {"trade_id": "x1", "exchange": "bybit", "symbol": "BTCUSDT",
                          "direction": "Long", "entry": 1, "exit": 2, "qty": 1, "pnl": 1})
    db.delete_user(uid)
    assert db.get_trades(uid) == []
    assert db.list_connections(uid) == []
