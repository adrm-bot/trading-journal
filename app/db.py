"""db.py — SQLite 멀티유저 저장. 시크릿은 connections.data_enc 봉투암호화로만."""
import json
import os
import sqlite3
import time

from . import crypto

DB_PATH = os.getenv("APP_DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

TRADE_COLS = ["exchange", "symbol", "direction", "entry", "exit", "qty", "pnl",
              "opened_at", "closed_at", "fees", "funding", "leverage", "fill_count",
              "liquidated", "exit_reason", "status", "plan", "setup", "sl", "emotion", "memo",
              "exit_count", "exit_legs", "mae_price", "mfe_price",
              "point_value"]  # 분할청산 레그 + 최대 역행/순행(D) + 선물 포인트가치(NT — 달러 환산용)
_INTENT = {"plan", "setup", "strategy", "sl", "tp", "tp2", "tp3", "emotion", "memo",
           "review", "mistake_tag", "chart_url", "status",
           "setup_grade", "exec_grade", "conviction"}  # 자기채점: 셋업 A/B/C·실행 A~F·확신 1~5


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
          id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, created INTEGER,
          plan TEXT DEFAULT 'free', stripe_customer_id TEXT,
          account_equity REAL, be_pct REAL);
        CREATE TABLE IF NOT EXISTS connections(
          user_id INTEGER, kind TEXT, data_enc TEXT, updated INTEGER,
          PRIMARY KEY(user_id, kind));
        CREATE TABLE IF NOT EXISTS position_intents(
          user_id INTEGER, exchange TEXT, symbol TEXT, direction TEXT,
          plan TEXT, setup TEXT, strategy TEXT, sl REAL, tp REAL, tp2 REAL, tp3 REAL,
          emotion TEXT, memo TEXT, conviction INTEGER,
          entry_snap REAL, created INTEGER,
          PRIMARY KEY(user_id, exchange, symbol, direction));
        CREATE TABLE IF NOT EXISTS trades(
          user_id INTEGER, trade_id TEXT,
          exchange TEXT, symbol TEXT, direction TEXT, entry REAL, exit REAL, qty REAL, pnl REAL,
          opened_at TEXT, closed_at TEXT, fees REAL, funding REAL, leverage REAL,
          fill_count INTEGER, liquidated INTEGER DEFAULT 0, exit_reason TEXT,
          status TEXT DEFAULT '의도 미기입',
          plan TEXT, setup TEXT, strategy TEXT, sl REAL, tp REAL, tp2 REAL, tp3 REAL, emotion TEXT, memo TEXT,
          review TEXT, mistake_tag TEXT, chart_url TEXT,
          setup_grade TEXT, exec_grade TEXT, conviction INTEGER,
          exit_count INTEGER, exit_legs TEXT, mae_price REAL, mfe_price REAL, point_value REAL,
          PRIMARY KEY(user_id, trade_id));
        """)
        # 마이그레이션: 기존 DB에 누락 컬럼 추가 (idempotent)
        cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
        for name, typ in (("tp", "REAL"), ("strategy", "TEXT"), ("tp2", "REAL"), ("tp3", "REAL"),
                          ("review", "TEXT"), ("mistake_tag", "TEXT"), ("chart_url", "TEXT"),
                          ("setup_grade", "TEXT"), ("exec_grade", "TEXT"), ("conviction", "INTEGER"),
                          ("exit_count", "INTEGER"), ("exit_legs", "TEXT"),
                          ("mae_price", "REAL"), ("mfe_price", "REAL"), ("preplanned", "INTEGER"),
                          ("point_value", "REAL"),
                          ("opened_at", "TEXT"), ("fees", "REAL"), ("funding", "REAL"),
                          ("leverage", "REAL"), ("fill_count", "INTEGER"), ("liquidated", "INTEGER"),
                          ("exit_reason", "TEXT")):
            if name not in cols:
                c.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ}")
        # users 마이그레이션: 결제 스캐폴드 컬럼(멱등)
        ucols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        for name, typ in (("plan", "TEXT DEFAULT 'free'"), ("stripe_customer_id", "TEXT"),
                          ("account_equity", "REAL"), ("be_pct", "REAL")):
            if name not in ucols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {typ}")
        # 백필: 기존 강제청산 행에 청산기준 부여(멱등)
        c.execute("UPDATE trades SET exit_reason='liquidation' WHERE exit_reason IS NULL AND liquidated=1")
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


def get_user_plan(uid):
    with conn() as c:
        r = c.execute("SELECT plan FROM users WHERE id=?", (uid,)).fetchone()
    return (r["plan"] if r and r["plan"] else "free")


def get_user_settings(uid):
    """트레이딩 설정 — 계좌 자산(USDT)·본절 밴드(%). 미설정이면 None/기본."""
    with conn() as c:
        r = c.execute("SELECT account_equity, be_pct FROM users WHERE id=?", (uid,)).fetchone()
    eq = r["account_equity"] if r else None
    be = r["be_pct"] if r and r["be_pct"] is not None else None
    return {"account_equity": eq, "be_pct": be}


def set_user_settings(uid, account_equity=None, be_pct=None):
    with conn() as c:
        c.execute("UPDATE users SET account_equity=?, be_pct=? WHERE id=?", (account_equity, be_pct, uid))


def delete_user(uid):
    """계정·데이터 완전 삭제 (PIPA/GDPR 삭제권). 거래·연동·유저 전부 제거 — 되돌릴 수 없음."""
    with conn() as c:
        c.execute("DELETE FROM trades WHERE user_id=?", (uid,))
        c.execute("DELETE FROM connections WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))


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


def get_trade(uid, trade_id):
    with conn() as c:
        r = c.execute("SELECT * FROM trades WHERE user_id=? AND trade_id=?", (uid, trade_id)).fetchone()
    return dict(r) if r else None


# --- 사전 계획(보유 중 포지션) — (거래소·심볼·방향)당 1건, 덮어쓰기=수정. 청산 적재 시 소비 ---
_PINTENT_FIELDS = ["plan", "setup", "strategy", "sl", "tp", "tp2", "tp3",
                   "emotion", "memo", "conviction", "entry_snap"]


def set_position_intent(uid, exchange, symbol, direction, fields: dict):
    cols = ",".join(_PINTENT_FIELDS)
    ph = ",".join("?" for _ in _PINTENT_FIELDS)
    with conn() as c:
        c.execute(f"INSERT OR REPLACE INTO position_intents(user_id,exchange,symbol,direction,{cols},created) "
                  f"VALUES(?,?,?,?,{ph},?)",
                  [uid, exchange, symbol, direction, *[fields.get(k) for k in _PINTENT_FIELDS], int(time.time())])


def get_position_intents(uid):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM position_intents WHERE user_id=?", (uid,))]


def delete_position_intent(uid, exchange, symbol, direction) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM position_intents WHERE user_id=? AND exchange=? AND symbol=? AND direction=?",
                        (uid, exchange, symbol, direction))
        return cur.rowcount > 0


def mark_preplanned(uid, trade_id):
    """사전 계획이 자동 적용된 거래 표식. _INTENT 밖이라 /api/intent로는 못 세움 — 사후 세탁 불가."""
    with conn() as c:
        c.execute("UPDATE trades SET preplanned=1 WHERE user_id=? AND trade_id=?", (uid, trade_id))


# 일괄 기입에서 금지하는 필드: strategy를 한 번에 박으면 충동거래가 '계획 전략거래'로
# 둔갑해 전략별 통계가 세탁됨(안티-조작 약속 위배). 전략은 거래별 개별 기입만 허용.
_BULK_FORBIDDEN = {"status", "strategy", "setup", "sl", "tp", "tp2", "tp3", "mistake_tag", "review",
                   "setup_grade", "exec_grade", "conviction"}  # 채점 일괄기입 금지(통계 세탁 방지)


def bulk_fill_unplanned(uid, fields: dict) -> int:
    """status='의도 미기입' 거래를 일괄 기입(과거 정리용). 빈 필드·금지 필드는 건너뜀, status→기록완료. 반환=건수."""
    sets = {}
    for k, v in fields.items():
        if k not in _INTENT or k in _BULK_FORBIDDEN:
            continue
        if v is None or (isinstance(v, str) and v.strip() == ""):
            continue
        sets[k] = v
    sets["status"] = "기록완료"
    clause = ", ".join(f"{k}=?" for k in sets)
    with conn() as c:
        cur = c.execute(f"UPDATE trades SET {clause} WHERE user_id=? AND status='의도 미기입'",
                        [*sets.values(), uid])
        return cur.rowcount


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
