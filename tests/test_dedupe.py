"""포지션 중복 방지·정리 — 재구성 trade_id 불안정(Bybit updatedTime 갱신)으로 같은 포지션이
새 trade_id로 재적재되던 버그의 회귀 방지. 신원=(user,exchange,symbol,direction,opened_at)."""
import os
from cryptography.fernet import Fernet

os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from app import db  # noqa: E402


def _fresh(tmp_path):
    db.DB_PATH = str(tmp_path / "t.db")
    db.init()
    return db.upsert_user("a@b.c", "A")


def _pos(tid, **kw):
    base = {"trade_id": tid, "exchange": "bybit", "symbol": "BTCUSDT", "direction": "Long",
            "entry": 45000.0, "exit": 45500.0, "qty": 0.1, "pnl": 50.0,
            "opened_at": "2026-07-01 10:00:00", "closed_at": "2026-07-01 12:00:00"}
    base.update(kw)
    return base


def test_same_position_diff_tradeid_no_duplicate(tmp_path):
    uid = _fresh(tmp_path)
    # 1차 적재: trade_id가 마지막 청산주문 A
    assert db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:A")) == 1
    # 재풀링: updatedTime 갱신으로 trade_id만 B로 바뀌고 내용은 동일한 같은 포지션
    assert db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:B")) == 0  # 신규 아님
    rows = db.get_trades(uid)
    assert len(rows) == 1  # 중복 안 쌓임


def test_growth_updates_market_fields_preserves_intent(tmp_path):
    uid = _fresh(tmp_path)
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:A"))
    db.update_intent(uid, "bybit:pos:BTCUSDT:A", {"plan": "눌림 매수", "review": "계획대로", "status": "복기완료"})
    # 같은 포지션이 더 큰 청산으로 갱신되어 재등장(pnl·exit 변화, trade_id도 변함)
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:B", exit=45800.0, pnl=80.0, qty=0.1))
    rows = db.get_trades(uid)
    assert len(rows) == 1
    r = rows[0]
    assert r["pnl"] == 80.0 and r["exit"] == 45800.0   # 시장값은 갱신
    assert r["plan"] == "눌림 매수" and r["status"] == "복기완료"  # 의도는 보존


def test_distinct_positions_not_merged(tmp_path):
    uid = _fresh(tmp_path)
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:A"))
    # 다른 오픈시각 = 다른 포지션 → 별개 유지
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:C", opened_at="2026-07-02 09:00:00"))
    # 다른 방향도 별개
    db.upsert_trade(uid, _pos("bybit:pos:BTCUSDT:D", direction="Short"))
    assert len(db.get_trades(uid)) == 3


def test_shifted_open_same_close_cycle_updates_annotated_row(tmp_path):
    uid = _fresh(tmp_path)
    old = _pos("binance:pos:BTCUSDT:100", exchange="binance")
    db.upsert_trade(uid, old)
    db.update_intent(uid, old["trade_id"], {"plan": "경계 전 계획", "status": "기록완료"})
    # 과거 조회창 경계 때문에 진입시각·마지막 체결 id·집계 수량이 조금 달라진 동일 청산 사이클
    new = _pos("binance:pos:BTCUSDT:132", exchange="binance",
               opened_at="2026-07-01 10:05:00", entry=45120.0, exit=45520.0,
               qty=0.11, pnl=52.0)
    assert db.upsert_trade(uid, new) == 0
    rows = db.get_trades(uid)
    assert len(rows) == 1
    assert rows[0]["plan"] == "경계 전 계획"
    assert rows[0]["pnl"] == 52.0 and rows[0]["qty"] == 0.11
    assert rows[0]["opened_at"] == "2026-07-01 10:05:00"


def test_near_values_different_close_second_remain_distinct(tmp_path):
    uid = _fresh(tmp_path)
    db.upsert_trade(uid, _pos("binance:pos:BTCUSDT:100", exchange="binance"))
    later = _pos("binance:pos:BTCUSDT:101", exchange="binance",
                 opened_at="2026-07-01 12:00:01", closed_at="2026-07-01 12:00:01")
    assert db.upsert_trade(uid, later) == 1
    assert len(db.get_trades(uid)) == 2


def test_semantic_startup_dedupe_merges_shifted_close_cycle_and_all_annotations(tmp_path):
    uid = _fresh(tmp_path)
    a = _pos("binance:pos:ONDOUSDT:1", exchange="binance", symbol="ONDOUSDT",
             opened_at="2026-07-01 08:00:00", entry=0.91, exit=0.87, qty=45000, pnl=-1800)
    b = _pos("binance:pos:ONDOUSDT:2", exchange="binance", symbol="ONDOUSDT",
             opened_at="2026-07-01 08:04:00", entry=0.912, exit=0.871, qty=44800, pnl=-1790)
    # upsert의 실시간 방어를 우회해 과거 버전이 이미 만든 중복 두 행을 재현한다.
    with db.conn() as c:
        cols = ", ".join(db.TRADE_COLS)
        ph = ", ".join("?" for _ in db.TRADE_COLS)
        for row in (a, b):
            c.execute(f"INSERT INTO trades(user_id,trade_id,{cols}) VALUES(?,?,{ph})",
                      [uid, row["trade_id"], *[row.get(k) for k in db.TRADE_COLS]])
    db.update_intent(uid, a["trade_id"], {"plan": "초기 계획", "status": "기록완료"})
    db.update_intent(uid, b["trade_id"], {"review": "복기 내용", "status": "복기완료"})
    assert db.dedupe_close_cycles() == 1
    rows = db.get_trades(uid)
    assert len(rows) == 1
    assert rows[0]["opened_at"] == "2026-07-01 08:00:00"  # 더 긴 시장 문맥
    assert rows[0]["plan"] == "초기 계획" and rows[0]["review"] == "복기 내용"
    assert rows[0]["status"] == "복기완료"
    assert db.dedupe_close_cycles() == 0


def test_no_opened_at_falls_back_to_tradeid(tmp_path):
    uid = _fresh(tmp_path)
    # opened_at 없는 레거시/테스트 경로 — trade_id 기준 멱등
    t = {"trade_id": "x1", "exchange": "bybit", "symbol": "ETHUSDT", "direction": "Long",
         "entry": 1, "exit": 2, "qty": 1, "pnl": 1}
    assert db.upsert_trade(uid, t) == 1
    assert db.upsert_trade(uid, t) == 0
    assert len(db.get_trades(uid)) == 1


def test_dedupe_migration_collapses_and_keeps_annotated(tmp_path):
    uid = _fresh(tmp_path)
    # 신원이 같은데 서로 다른 trade_id로 이미 쌓인 3중복을 직접 심는다(과거 버그 재현)
    for tid in ("A", "B", "C"):
        db.upsert_trade(uid, _pos("nodedup:" + tid + ":raw"))  # opened_at 없는 트릭 불가 → 아래로
    # 위 3건은 신원 dedupe로 이미 1건이 됨 → 과거 버그를 그대로 재현하려면 직삽입
    with db.conn() as c:
        c.execute("DELETE FROM trades WHERE user_id=?", (uid,))
        for tid in ("A", "B", "C"):
            cols = ", ".join(db.TRADE_COLS)
            ph = ", ".join("?" for _ in db.TRADE_COLS)
            p = _pos("bybit:pos:BTCUSDT:" + tid)
            c.execute(f"INSERT INTO trades(user_id,trade_id,{cols}) VALUES(?,?,{ph})",
                      [uid, p["trade_id"], *[p.get(k) for k in db.TRADE_COLS]])
    db.update_intent(uid, "bybit:pos:BTCUSDT:B", {"plan": "주석 있는 행", "status": "복기완료"})
    assert len(db.get_trades(uid)) == 3           # 중복 3건 존재(버그 상태)
    removed = db.dedupe_positions()
    assert removed == 2                            # 2건 삭제
    rows = db.get_trades(uid)
    assert len(rows) == 1
    assert rows[0]["plan"] == "주석 있는 행"       # 주석 있는 행이 살아남음
    # 멱등 — 다시 돌려도 0
    assert db.dedupe_positions() == 0
