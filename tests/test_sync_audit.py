"""거래소 원장 정합성 상태는 적재와 분리해 영속화한다."""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from app import db, engine  # noqa: E402


def _fresh(tmp_path):
    db.DB_PATH = str(tmp_path / "audit.db")
    db.init()
    uid = db.upsert_user("audit@example.com", "Audit")
    db.set_connection(uid, "binance", {"key": "k" * 10, "secret": "s" * 20})
    return uid


def test_sync_audit_roundtrip_and_disconnect_cleanup(tmp_path):
    uid = _fresh(tmp_path)
    details = {"symbol_count": 1, "ledger_total": 22_000.0,
               "fills_total": 22_000.0, "delta": 0.0}
    db.set_sync_audit(uid, "binance", "matched", details)
    row = db.get_sync_audits(uid)[0]
    assert row["exchange"] == "binance"
    assert row["status"] == "matched"
    assert row["details"] == details
    assert row["checked_at"] > 0

    db.delete_connection(uid, "binance")
    assert db.get_sync_audits(uid) == []


def test_pull_persists_matched_binance_ledger(tmp_path, monkeypatch):
    uid = _fresh(tmp_path)

    def fake_fetch(key, secret, lookback, audit_sink=None):
        audit_sink.append({"symbol": "BTCUSDT", "ledger": 22_000.0,
                           "fills": 22_000.0, "delta": 0.0})
        return []

    monkeypatch.setattr(engine, "fetch_binance", fake_fetch)
    result = engine.pull_user(uid)
    assert result["binance"]["audit_status"] == "matched"
    audit = db.get_sync_audits(uid)[0]
    assert audit["status"] == "matched"
    assert audit["details"]["ledger_total"] == 22_000.0


def test_mismatch_blocks_import_and_persists_warning(tmp_path, monkeypatch):
    uid = _fresh(tmp_path)
    existing = {"trade_id": "binance:pos:BTCUSDT:old", "exchange": "binance",
                "symbol": "BTCUSDT", "direction": "Long", "entry": 1, "exit": 2,
                "qty": 1, "pnl": 100, "opened_at": "2026-01-01 00:00:00",
                "closed_at": "2026-01-02 00:00:00"}
    db.upsert_trade(uid, existing)

    def fake_fetch(key, secret, lookback, audit_sink=None):
        audit = {"symbol": "BTCUSDT", "ledger": 22_000.0,
                 "fills": 50_400.0, "delta": 28_400.0}
        audit_sink.append(audit)
        raise engine.RealizedPnlMismatch(audit)

    monkeypatch.setattr(engine, "fetch_binance", fake_fetch)
    result = engine.pull_user(uid)
    assert result["binance"]["audit_status"] == "mismatch"
    assert "기존 일지는 유지" in result["binance"]["error"]
    assert [t["trade_id"] for t in db.get_trades(uid)] == [existing["trade_id"]]
    audit = db.get_sync_audits(uid)[0]
    assert audit["status"] == "mismatch"
    assert audit["details"]["delta"] == 28_400.0

    # 대상 없음/일시 조회 실패가 기존 불일치를 조용히 지우면 안 된다.
    monkeypatch.setattr(engine, "fetch_binance",
                        lambda key, secret, lookback, audit_sink=None: [])
    no_data = engine.pull_user(uid)
    assert no_data["binance"]["audit_status"] == "mismatch"
    assert db.get_sync_audits(uid)[0]["status"] == "mismatch"

    # 같은 조회 범위를 완전히 대조해 일치한 경우에만 경고를 해제한다.
    def matched_fetch(key, secret, lookback, audit_sink=None):
        audit_sink.append({"symbol": "BTCUSDT", "ledger": 22_000.0,
                           "fills": 22_000.0, "delta": 0.0})
        return []

    monkeypatch.setattr(engine, "fetch_binance", matched_fetch)
    matched = engine.pull_user(uid)
    assert matched["binance"]["audit_status"] == "matched"
    assert db.get_sync_audits(uid)[0]["status"] == "matched"
