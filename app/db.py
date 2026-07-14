"""db.py — SQLite 멀티유저 저장. 시크릿은 connections.data_enc 봉투암호화로만."""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import crypto

DB_PATH = os.getenv("APP_DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

TRADE_COLS = ["exchange", "symbol", "direction", "entry", "exit", "qty", "pnl",
              "opened_at", "closed_at", "fees", "funding", "leverage", "fill_count",
              "liquidated", "exit_reason", "status", "plan", "setup", "sl", "emotion", "memo",
              "exit_count", "raw_exit_count", "exit_legs", "mae_price", "mfe_price",
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
          account_equity REAL, be_pct REAL, journal_since TEXT);
        CREATE TABLE IF NOT EXISTS connections(
          user_id INTEGER, kind TEXT, data_enc TEXT, updated INTEGER,
          PRIMARY KEY(user_id, kind));
        CREATE TABLE IF NOT EXISTS liqmap_watchlists(
          user_id INTEGER, provider TEXT, symbol TEXT, sort_order INTEGER, updated INTEGER,
          PRIMARY KEY(user_id, provider, symbol));
        CREATE TABLE IF NOT EXISTS sync_audits(
          user_id INTEGER, exchange TEXT, status TEXT, details_json TEXT, checked_at INTEGER,
          PRIMARY KEY(user_id, exchange));
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
          exit_count INTEGER, raw_exit_count INTEGER, exit_legs TEXT, mae_price REAL, mfe_price REAL, point_value REAL,
          PRIMARY KEY(user_id, trade_id));
        """)
        # 마이그레이션: 기존 DB에 누락 컬럼 추가 (idempotent)
        cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
        for name, typ in (("tp", "REAL"), ("strategy", "TEXT"), ("tp2", "REAL"), ("tp3", "REAL"),
                          ("review", "TEXT"), ("mistake_tag", "TEXT"), ("chart_url", "TEXT"),
                          ("setup_grade", "TEXT"), ("exec_grade", "TEXT"), ("conviction", "INTEGER"),
                          ("exit_count", "INTEGER"), ("raw_exit_count", "INTEGER"), ("exit_legs", "TEXT"),
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
                          ("account_equity", "REAL"), ("be_pct", "REAL"),
                          ("journal_since", "TEXT")):
            if name not in ucols:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {typ}")
        # 백필: 기존 강제청산 행에 청산기준 부여(멱등)
        c.execute("UPDATE trades SET exit_reason='liquidation' WHERE exit_reason IS NULL AND liquidated=1")
        # 컷오버: 포지션 단위(v2) 이전의 '닫는 주문 1건=1행' 레거시 거래소 행 제거.
        # 새 키는 'exchange:pos:...' 형태라 LIKE로 구분. 사후 의도는 거의 없던 초기 데이터.
        c.execute("DELETE FROM trades WHERE (trade_id LIKE 'bybit:%' OR trade_id LIKE 'binance:%') "
                  "AND trade_id NOT LIKE '%:pos:%'")
    # 재구성 trade_id 불안정으로 쌓인 같은-포지션 중복 정리(멱등) — 통계 부풀림 교정
    removed = dedupe_positions() + dedupe_close_cycles()
    if removed:
        import logging
        logging.getLogger("app.db").warning("중복 포지션 %d건 정리", removed)


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
    """트레이딩 설정 — 계좌 자산·본절 밴드·일지 표시 시작일."""
    with conn() as c:
        r = c.execute(
            "SELECT account_equity, be_pct, journal_since FROM users WHERE id=?", (uid,)
        ).fetchone()
    eq = r["account_equity"] if r else None
    be = r["be_pct"] if r and r["be_pct"] is not None else None
    since = r["journal_since"] if r and r["journal_since"] else None
    return {"account_equity": eq, "be_pct": be, "journal_since": since}


def set_user_settings(uid, account_equity=None, be_pct=None, journal_since=None):
    with conn() as c:
        c.execute(
            "UPDATE users SET account_equity=?, be_pct=?, journal_since=? WHERE id=?",
            (account_equity, be_pct, journal_since, uid),
        )


def reset_journal(uid, journal_since=None):
    """사용자 일지만 초기화한다. 계정·연결·워치리스트·트레이딩 설정은 보존."""
    with conn() as c:
        trades = c.execute("DELETE FROM trades WHERE user_id=?", (uid,)).rowcount
        intents = c.execute("DELETE FROM position_intents WHERE user_id=?", (uid,)).rowcount
        audits = c.execute("DELETE FROM sync_audits WHERE user_id=?", (uid,)).rowcount
        c.execute("UPDATE users SET journal_since=? WHERE id=?", (journal_since, uid))
    return {"trades": trades, "position_intents": intents, "sync_audits": audits}


def delete_user(uid):
    """계정·데이터 완전 삭제 (PIPA/GDPR 삭제권). 거래·연동·유저 전부 제거 — 되돌릴 수 없음."""
    with conn() as c:
        c.execute("DELETE FROM trades WHERE user_id=?", (uid,))
        c.execute("DELETE FROM position_intents WHERE user_id=?", (uid,))
        c.execute("DELETE FROM connections WHERE user_id=?", (uid,))
        c.execute("DELETE FROM liqmap_watchlists WHERE user_id=?", (uid,))
        c.execute("DELETE FROM sync_audits WHERE user_id=?", (uid,))
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
        c.execute("DELETE FROM sync_audits WHERE user_id=? AND exchange=?", (uid, kind))


# --- 거래소 원장 정합성 감사 ---
def set_sync_audit(uid, exchange, status, details=None):
    """마지막 원장 대조 결과를 사용자·거래소별로 보존한다.

    거래 데이터와 분리해 pull 실패나 새로고침 뒤에도 정합성 상태를 숨기지 않는다.
    """
    payload = json.dumps(details or {}, ensure_ascii=False, separators=(",", ":"))
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO sync_audits(user_id,exchange,status,details_json,checked_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,exchange) DO UPDATE SET "
            "status=excluded.status, details_json=excluded.details_json, "
            "checked_at=excluded.checked_at",
            (uid, exchange, status, payload, now),
        )


def get_sync_audits(uid):
    with conn() as c:
        rows = c.execute(
            "SELECT exchange,status,details_json,checked_at FROM sync_audits "
            "WHERE user_id=? ORDER BY exchange", (uid,)
        ).fetchall()
    out = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError):
            details = {}
        out.append({"exchange": row["exchange"], "status": row["status"],
                    "checked_at": row["checked_at"], "details": details})
    return out


# --- 예측형 청산맵 추적 심볼 (API 키와 독립적으로 유지) ---
def get_liqmap_watchlist(uid, provider):
    with conn() as c:
        return [r["symbol"] for r in c.execute(
            "SELECT symbol FROM liqmap_watchlists WHERE user_id=? AND provider=? "
            "ORDER BY sort_order, rowid", (uid, provider))]


def set_liqmap_watchlist(uid, provider, symbols):
    """공급자별 순서를 보존해 목록 전체를 원자적으로 교체한다."""
    now = int(time.time())
    with conn() as c:
        c.execute("DELETE FROM liqmap_watchlists WHERE user_id=? AND provider=?", (uid, provider))
        c.executemany(
            "INSERT INTO liqmap_watchlists(user_id,provider,symbol,sort_order,updated) "
            "VALUES(?,?,?,?,?)",
            [(uid, provider, symbol, i, now) for i, symbol in enumerate(symbols)])


# --- trades ---
# 포지션 신원 = (user_id, exchange, symbol, direction, opened_at). opened_at은 첫 체결시각(createdTime
# 기반)이라 재풀링에도 안 바뀜. trade_id는 거래소 재구성 특성상 불안정(Bybit updatedTime 갱신 시 마지막
# 청산주문 id가 바뀌어 같은 포지션이 새 trade_id로 잡힘) → 신원 기준으로 dedupe해야 중복 적재를 막는다.
# 갱신 시 아래 시장 파생 컬럼만 덮어쓰고 유저 의도(status·plan·setup·sl·emotion·memo)는 절대 안 건드림.
_MARKET_COLS = ["entry", "exit", "qty", "pnl", "closed_at", "fees", "funding", "leverage",
                "fill_count", "liquidated", "exit_reason", "exit_count", "raw_exit_count", "exit_legs",
                "mae_price", "mfe_price", "point_value"]


def _near(a, b, rel=0.01, floor=1e-9):
    try:
        aa, bb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(aa - bb) <= max(abs(aa), abs(bb), floor) * rel


def _same_close_cycle(old: dict, new: dict) -> bool:
    """재구성 경계가 달라져 opened_at/trade_id가 흔들린 같은 청산 사이클을 보수적으로 판별.

    동일 초에 같은 심볼·방향 포지션이 닫히고 가격·수량·손익까지 유사해야 한다. 빠른 재진입을
    임의로 합치지 않기 위해 closed_at이 한 글자라도 다르면 매칭하지 않는다.
    """
    if not old.get("closed_at") or old.get("closed_at") != new.get("closed_at"):
        return False
    try:
        closed = datetime.fromisoformat(str(new["closed_at"]))
        if datetime.fromisoformat(str(old.get("opened_at"))) > closed \
                or datetime.fromisoformat(str(new.get("opened_at"))) > closed:
            return False
    except (TypeError, ValueError):
        return False
    if not (_near(old.get("entry"), new.get("entry"), 0.01)
            and _near(old.get("exit"), new.get("exit"), 0.01)
            and _near(old.get("qty"), new.get("qty"), 0.30)):
        return False
    op, np = old.get("pnl"), new.get("pnl")
    try:
        if float(op or 0) * float(np or 0) < 0:
            return False
    except (TypeError, ValueError):
        return False
    return _near(op, np, 0.30, floor=10.0)


def upsert_trade(uid, t: dict) -> int:
    """신규 삽입이면 1, 이미 있으면 0. 같은 포지션 신원이 이미 있으면 시장 파생값만 갱신(의도 보존).
    opened_at이 없으면(레거시/테스트) trade_id 기준 INSERT OR IGNORE 폴백."""
    cols = ", ".join(TRADE_COLS)
    ph = ", ".join("?" for _ in TRADE_COLS)
    vals = [t.get(k) for k in TRADE_COLS]
    oa = t.get("opened_at")
    with conn() as c:
        if oa:
            row = c.execute(
                "SELECT trade_id FROM trades WHERE user_id=? AND exchange=? AND symbol=? "
                "AND direction=? AND opened_at=? LIMIT 1",
                (uid, t.get("exchange"), t.get("symbol"), t.get("direction"), oa)).fetchone()
            if row:  # 같은 포지션 재등장 — 시장값만 갱신, 의도 보존, 중복 행 안 만듦
                sets = ", ".join(f"{k}=?" for k in _MARKET_COLS)
                c.execute(f"UPDATE trades SET {sets} WHERE user_id=? AND trade_id=?",
                          [*[t.get(k) for k in _MARKET_COLS], uid, row["trade_id"]])
                return 0
            # 조회창 경계가 포지션 중간을 잘랐던 과거 버전은 opened_at과 마지막 체결 id가 흔들릴 수
            # 있다. 동일 청산초 + 유사 시장값이면 기존 주석 행을 갱신해 중복 생성을 막는다.
            if t.get("closed_at"):
                candidates = c.execute(
                    "SELECT * FROM trades WHERE user_id=? AND exchange=? AND symbol=? "
                    "AND direction=? AND closed_at=?",
                    (uid, t.get("exchange"), t.get("symbol"), t.get("direction"),
                     t.get("closed_at"))).fetchall()
                same = next((r for r in candidates if _same_close_cycle(dict(r), t)), None)
                if same:
                    # 더 긴 조회 문맥으로 복원한 실제 첫 진입시각도 교정한다. trade_id와 사용자 주석은
                    # 유지해 기존 모달 링크·복기 기록이 끊기지 않게 한다.
                    sets = "opened_at=?, " + ", ".join(f"{k}=?" for k in _MARKET_COLS)
                    c.execute(f"UPDATE trades SET {sets} WHERE user_id=? AND trade_id=?",
                              [t.get("opened_at"), *[t.get(k) for k in _MARKET_COLS],
                               uid, same["trade_id"]])
                    return 0
        cur = c.execute(f"INSERT OR IGNORE INTO trades(user_id,trade_id,{cols}) VALUES(?,?,{ph})",
                        [uid, t["trade_id"], *vals])
        return cur.rowcount


def dedupe_positions() -> int:
    """재구성 trade_id 불안정으로 쌓인 '같은 포지션 신원' 중복 행 1회 정리(멱등).
    신원=(user_id,exchange,symbol,direction,opened_at). 그룹당 주석이 가장 풍부한 행 1개만 남기고 삭제
    (복기완료>기록완료>미기입, plan·review 있는 것 우선, 그다음 rowid). 반환=삭제 건수."""
    with conn() as c:
        cur = c.execute("""
            DELETE FROM trades WHERE rowid IN (
              SELECT rowid FROM (
                SELECT rowid, ROW_NUMBER() OVER (
                  PARTITION BY user_id, exchange, symbol, direction, opened_at
                  ORDER BY CASE status WHEN '복기완료' THEN 0 WHEN '기록완료' THEN 1 ELSE 2 END,
                           (plan IS NULL OR plan=''), (review IS NULL OR review=''), rowid
                ) rn
                FROM trades WHERE opened_at IS NOT NULL
              ) WHERE rn > 1
            )""")
        return cur.rowcount


def dedupe_close_cycles() -> int:
    """과거 경계 버그로 opened_at만 달라진 동일 청산 사이클을 보수적으로 정리한다.

    동일 사용자·거래소·심볼·방향·청산초 안에서 가격·수량·손익까지 유사한 행만 묶는다.
    사용자 주석은 구성원 전체에서 합쳐 보존하고, 시장값은 가장 이른 진입시각(문맥이 가장 긴 행)을
    기준으로 교정한다. 조건이 하나라도 불명확하면 건드리지 않는다.
    """
    removed = 0
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT rowid AS _rowid,* FROM trades WHERE opened_at IS NOT NULL "
            "AND closed_at IS NOT NULL AND trade_id LIKE '%:pos:%' "
            "ORDER BY user_id,exchange,symbol,direction,closed_at,rowid"
        )]
        groups = {}
        for r in rows:
            key = (r.get("user_id"), r.get("exchange"), r.get("symbol"),
                   r.get("direction"), r.get("closed_at"))
            groups.setdefault(key, []).append(r)

        status_rank = {"복기완료": 0, "기록완료": 1, "의도 미기입": 2}
        for group in groups.values():
            if len(group) < 2:
                continue
            pending = list(group)
            components = []
            while pending:
                comp = [pending.pop(0)]
                changed = True
                while changed:
                    changed = False
                    for candidate in pending[:]:
                        if any(_same_close_cycle(member, candidate) for member in comp):
                            comp.append(candidate)
                            pending.remove(candidate)
                            changed = True
                components.append(comp)

            for comp in components:
                if len(comp) < 2:
                    continue
                rich = sorted(comp, key=lambda r: (
                    status_rank.get(r.get("status"), 3),
                    -sum(1 for k in _INTENT if k != "status" and r.get(k) not in (None, "")),
                    r.get("_rowid") or 0,
                ))
                keeper = rich[0]
                market = min(comp, key=lambda r: (
                    str(r.get("opened_at") or "9999"),
                    -int(r.get("fill_count") or 0),
                    r.get("_rowid") or 0,
                ))

                updates = {"opened_at": market.get("opened_at")}
                updates.update({k: market.get(k) for k in _MARKET_COLS})
                for field in (_INTENT - {"status"}):
                    val = next((r.get(field) for r in rich if r.get(field) not in (None, "")), None)
                    if val is not None:
                        updates[field] = val
                updates["status"] = rich[0].get("status") or "의도 미기입"
                if any(r.get("preplanned") for r in comp):
                    updates["preplanned"] = 1

                sets = ", ".join(f"{k}=?" for k in updates)
                c.execute(f"UPDATE trades SET {sets} WHERE user_id=? AND trade_id=?",
                          [*updates.values(), keeper["user_id"], keeper["trade_id"]])
                doomed = [r["trade_id"] for r in comp if r["trade_id"] != keeper["trade_id"]]
                c.executemany("DELETE FROM trades WHERE user_id=? AND trade_id=?",
                              [(keeper["user_id"], tid) for tid in doomed])
                removed += len(doomed)
    return removed


def delete_auto_trades(uid, kind, since=None, unannotated_only=False) -> int:
    """자동 임포트 포지션(trade_id 'kind:pos:%') 삭제 — 재적재 시 겹침 누적 제거용.
    since: closed_at >= since(문자열)인 것만(윈도 한정). unannotated_only: 손 안 댄 미기입 행만(주석 보존).
    반환=삭제 건수. 수동 사전계획(position_intents)·설정은 건드리지 않음."""
    q = "DELETE FROM trades WHERE user_id=? AND trade_id LIKE ?"
    args = [uid, f"{kind}:pos:%"]
    if since:
        q += " AND closed_at >= ?"
        args.append(since)
    if unannotated_only:  # status가 미기입이면 유저가 아무 의도도 안 넣은 순수 자동행
        q += " AND status='의도 미기입'"
    with conn() as c:
        return c.execute(q, args).rowcount


def _journal_since_boundary(since):
    """KST 날짜(YYYY-MM-DD)를 거래 원장의 UTC-naive 시작 시각으로 바꾼다."""
    if not since or len(str(since)) != 10:
        return since
    try:
        kst_midnight = datetime.fromisoformat(str(since)).replace(
            tzinfo=timezone(timedelta(hours=9))
        )
    except ValueError:
        return since
    return kst_midnight.astimezone(timezone.utc).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def get_trades(uid, since=None):
    q = "SELECT * FROM trades WHERE user_id=?"
    args = [uid]
    if since:
        q += " AND COALESCE(NULLIF(closed_at,''),NULLIF(opened_at,''),'') >= ?"
        args.append(_journal_since_boundary(since))
    q += " ORDER BY closed_at DESC"
    with conn() as c:
        return [dict(r) for r in c.execute(q, args)]


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
