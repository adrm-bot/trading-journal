from pathlib import Path

from app import db, engine


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "app.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def _trade(tid, closed_at, pnl=1.0):
    return {
        "trade_id": tid,
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "direction": "Long",
        "entry": 100.0,
        "exit": 101.0,
        "qty": 1.0,
        "pnl": pnl,
        "opened_at": closed_at.replace("12:00:00", "11:00:00"),
        "closed_at": closed_at,
        "status": "기록완료",
    }


def test_journal_since_filters_visible_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "journal.db"))
    db.init()
    uid = db.upsert_user("bapk14@gmail.com", "건웅")
    db.upsert_trade(uid, _trade("old", "2026-07-14 14:59:59", -10))
    db.upsert_trade(uid, _trade("start", "2026-07-14 15:00:00", 20))
    db.upsert_trade(uid, _trade("new", "2026-07-16 12:00:00", 30))

    db.set_user_settings(uid, 20_000, 0.1, "2026-07-15")
    settings = db.get_user_settings(uid)
    assert settings == {"account_equity": 20_000, "be_pct": 0.1,
                        "journal_since": "2026-07-15"}

    _, visible = engine.analyze_user(uid, since=settings["journal_since"])
    assert [row["trade_id"] for row in visible] == ["new", "start"]
    assert len(db.get_trades(uid)) == 3  # 날짜 설정은 원본을 삭제하지 않는다.


def test_reset_journal_preserves_account_connections_and_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "journal.db"))
    db.init()
    uid = db.upsert_user("bapk14@gmail.com", "건웅")
    db.set_user_settings(uid, 20_000, 0.1, None)
    db.upsert_trade(uid, _trade("one", "2026-07-14T12:00:00+00:00"))
    db.set_position_intent(uid, "binance", "BTCUSDT", "Long", {"plan": "테스트 계획"})
    db.set_sync_audit(uid, "binance", "mismatch", {"delta": 1})
    db.set_liqmap_watchlist(uid, "coinglass", ["BTCUSDT"])
    with db.conn() as c:
        c.execute(
            "INSERT INTO connections(user_id,kind,data_enc,updated) VALUES(?,?,?,?)",
            (uid, "binance", "opaque-test-value", 1),
        )

    removed = db.reset_journal(uid, "2026-07-15")

    assert removed == {"trades": 1, "position_intents": 1, "sync_audits": 1}
    assert db.get_trades(uid) == []
    assert db.get_position_intents(uid) == []
    assert db.get_sync_audits(uid) == []
    assert db.get_liqmap_watchlist(uid, "coinglass") == ["BTCUSDT"]
    assert db.list_connections(uid) == ["binance"]
    assert db.get_user_settings(uid) == {"account_equity": 20_000, "be_pct": 0.1,
                                         "journal_since": "2026-07-15"}


def test_settings_ui_and_reset_endpoint_are_wired():
    assert 'id="setJournalSince" type="date"' in HTML
    assert 'id="resetJournalBtn"' in HTML
    assert 'id="resetJournalBg"' in HTML
    assert 'postJSON("/api/journal/reset"' in HTML
    assert 'journal_since:$("#setJournalSince").value' in HTML
    assert '@app.post("/api/journal/reset")' in MAIN
    assert 'confirm") != "매매일지 초기화"' in MAIN
    assert "datetime.now(KST).date()" in MAIN
