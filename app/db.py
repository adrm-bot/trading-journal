"""db.py — SQLite 멀티유저 저장. 시크릿은 connections.data_enc 봉투암호화로만."""
import json
import os
import sqlite3
import time

from . import crypto

DB_PATH = os.getenv("APP_DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

TRADE_COLS = ["exchange", "symbol", "direction", "entry", "exit", "qty", "pnl",
              "opened_at", "closed_at", "fees", "funding", "leverage", "fill_count",
              "liquidated", "status", "plan", "setup", "sl", "emotion", "memo"]
_INTENT = {"plan", "setup", "strategy", "sl", "tp", "tp2", "emotion", "memo", "status"}


def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS connections(
          user_id INTEGER, kind TEXT, data_enc TEXT, updated INTEGER,
          PRIMARY KEY(user_id, kind));
        CREATE TABLE IF NOT EXISTS trades(
          user_id INTEGER, trade_id TEXT,
          exchange TEXT, symbol TEXT, direction TEXT, entry REAL, exit REAL, qty REAL, pnl REAL,
          opened_at TEXT, closed_at TEXT, fees REAL, funding REAL, leverage REAL,
          fill_count INTEGER, liquidated INTEGER DEFAULT 0,
          status TEXT DEFAULT '의도 미기입',
          plan TEXT, setup TEXT, strategy TEXT, sl REAL, tp REAL, tp2 REAL, emotion TEXT, memo TEXT,
          PRIMARY KEY(user_id, trade_id));
        """)
        # 마이그레이션: 기존 DB에 누락 컬럼 추가 (idempotent)
        cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
        for name, typ in (("tp", "REAL"), ("strategy", "TEXT"), ("tp2", "REAL"),
                          ("opened_at", "TEXT"), ("fees", "REAL"), ("funding", "REAL"),
                          ("leverage", "REAL"), ("fill_count", "INTEGER"), ("liquidated", "INTEGER")):
            if name not in cols:
                c.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ}")
        # 컷오버: 포지션 단위(v2) 이전의 '닫는 주문 1건=1행' 레거시 거래소 행 제거.
        # 새 키는 'exchange:pos:...' 형태라 LIKE로 구분. 사후 의도는 거의 없던 초기 데이터.
        c.execute("DELETE FROM trades WHERE (trade_id LIKE 'bybit:%' OR trade_id LIKE 'binance:%') "
                  "AND trade_id NOT LIKE '%:pos:%'")


# --- users ---
def upsert_user(email, name):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO users(email,name,created) VALUES(?,?,?)",
                  (email, name, int(time.time())))
        return c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


# --- connections (암호화) ---
def set_connection(uid, kind, data: dict):
    enc = crypto.encrypt(json.dumps(data))
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO connections(user_id,kind,data_enc,updated) VALUES(?,?,?,?)",
                  (uid, kind, enc, int(time.time())))


def get_connection(uid, kind):
    with conn() as c:
        r = c.execute("SELECT data_enc FROM connections WHERE user_id=? AND kind=?", (uid, kind)).fetchone()
    return json.loads(crypto.decrypt(r["data_enc"])) if r else None


def list_connections(uid):
    with conn() as c:
        return [r["kind"] for r in c.execute("SELECT kind FROM connections WHERE user_id=?", (uid,))]


def delete_connection(uid, kind):
    with conn() as c:
        c.execute("DELETE FROM connections WHERE user_id=? AND kind=?", (uid, kind))


# --- trades ---
def upsert_trade(uid, t: dict) -> int:
    """신규 삽입이면 1, 이미 있으면 0 (유저 의도는 보존 — INSERT OR IGNORE)."""
    cols = ", ".join(TRADE_COLS)
    ph = ", ".join("?" for _ in TRADE_COLS)
    vals = [t.get(k) for k in TRADE_COLS]
    with conn() as c:
        cur = c.execute(f"INSERT OR IGNORE INTO trades(user_id,trade_id,{cols}) VALUES(?,?,{ph})",
                        [uid, t["trade_id"], *vals])
        return cur.rowcount


def get_trades(uid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY closed_at DESC", (uid,))]


def update_intent(uid, trade_id, fields: dict) -> bool:
    """present 키만 반영(빈 문자열은 NULL로 clear). status는 빈 값이면 미반영."""
    sets = {}
    for k, v in fields.items():
        if k not in _INTENT:
            continue
        if k == "status" and not v:
            continue
        sets[k] = (None if (isinstance(v, str) and v.strip() == "") else v)
    if not sets:
        return False
    clause = ", ".join(f"{k}=?" for k in sets)
    with conn() as c:
        cur = c.execute(f"UPDATE trades SET {clause} WHERE user_id=? AND trade_id=?",
                        [*sets.values(), uid, trade_id])
        return cur.rowcount > 0
