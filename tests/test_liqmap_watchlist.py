"""유료 청산맵 공급자별 추적 심볼 — 키 없이 설정 가능하고 사용자 간 격리된다."""
import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from app import db, liqmap  # noqa: E402


def _fresh(tmp_path):
    db.DB_PATH = str(tmp_path / "liqwatch.db")
    db.init()
    return db.upsert_user("watch@example.com", "Watch")


def test_symbol_normalization_accepts_common_exchange_forms():
    assert liqmap.normalize_symbol("btc") == "BTCUSDT"
    assert liqmap.normalize_symbol("eth/usdt") == "ETHUSDT"
    assert liqmap.normalize_symbol("SOL-USDT:USDT") == "SOLUSDT"
    assert liqmap.normalize_symbol("1000pepeusdt") == "1000PEPEUSDT"
    assert liqmap.normalize_watchlist(["btc", "BTCUSDT", "eth-usdt"]) == [
        "BTCUSDT", "ETHUSDT"]


def test_watchlist_rejects_empty_invalid_and_oversized_lists():
    with pytest.raises(ValueError):
        liqmap.normalize_watchlist([])
    with pytest.raises(ValueError):
        liqmap.normalize_watchlist(["BTC$USDT"])
    with pytest.raises(ValueError):
        liqmap.normalize_watchlist([f"COIN{i}USDT" for i in range(liqmap.MAX_SYMBOLS + 1)])


def test_watchlist_is_ordered_and_scoped_by_provider_and_user(tmp_path):
    uid = _fresh(tmp_path)
    uid2 = db.upsert_user("other@example.com", "Other")
    db.set_liqmap_watchlist(uid, "kingfisher", ["SOLUSDT", "BTCUSDT"])
    db.set_liqmap_watchlist(uid, "coinglass", ["ETHUSDT", "HYPEUSDT"])
    db.set_liqmap_watchlist(uid2, "kingfisher", ["XRPUSDT"])

    assert db.get_liqmap_watchlist(uid, "kingfisher") == ["SOLUSDT", "BTCUSDT"]
    assert db.get_liqmap_watchlist(uid, "coinglass") == ["ETHUSDT", "HYPEUSDT"]
    assert db.get_liqmap_watchlist(uid2, "kingfisher") == ["XRPUSDT"]

    db.delete_user(uid)
    assert db.get_liqmap_watchlist(uid, "kingfisher") == []
    assert db.get_liqmap_watchlist(uid2, "kingfisher") == ["XRPUSDT"]


def test_coinglass_scaffold_never_guesses_a_paid_endpoint():
    assert liqmap.fetch_coinglass(None)["connected"] is False
    ready = liqmap.fetch_coinglass("paid-key-placeholder", "SOLUSDT")
    assert ready["connected"] is True
    assert ready["available"] is False
    assert ready["pair"] == "SOLUSDT"
