#!/usr/bin/env python3
"""main.py — 매매일지 멀티유저 웹앱 (프로덕션 하드닝).

로컬:  python -m uvicorn app.main:app --port 8000
배포:  uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*" --workers 1
시크릿은 SQLite에 봉투암호화로만 저장. read-only 거래소 키 권장.
"""
import csv
import io
import logging
import os
import secrets
import time

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **k): return False

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")

from . import db, engine, crypto, market, analytics  # noqa: E402

EXCH_NAMES = {"bybit": "Bybit", "binance": "Binance", "gate": "Gate.io",
              "notion": "Notion", "telegram": "Telegram", "sheets": "Google Sheets"}

# /api/pull 유저별 쿨다운(초) — 거래소 키 throttle/ban 방지 + 단일 워커 보호 (in-memory, 재시작 시 초기화)
PULL_COOLDOWN = int(os.getenv("PULL_COOLDOWN_SEC", "30"))
_pull_at: dict[int, float] = {}

# 결제 스캐폴드 — 기본 비활성(코드만 준비, 실제 과금 X). 켜려면 BILLING_ENABLED=true + Stripe 키.
BILLING_ENABLED = os.getenv("BILLING_ENABLED", "false").lower() == "true"
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "").strip()

APP_ENV = os.getenv("APP_ENV", "development").lower()
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

# --- fail-fast 보안 검증 (부팅 중단) ---
if APP_ENV == "production" and DEV_MODE:
    raise RuntimeError("프로덕션에서 DEV_MODE=true 금지 (무인증 dev-login이 열림)")

SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not SESSION_SECRET or len(SESSION_SECRET) < 32 or SESSION_SECRET == "dev-insecure-change-me":
    if DEV_MODE:
        SESSION_SECRET = secrets.token_hex(32)
        logger.warning("SESSION_SECRET 미설정 — DEV 임시 키 사용")
    else:
        raise RuntimeError("SESSION_SECRET 미설정/취약 (32바이트+ 랜덤 hex 필요)")

crypto.ensure_configured()  # APP_SECRET_KEY 유효성 즉시 검증
db.init()

app = FastAPI(title="매매일지")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   https_only=not DEV_MODE, same_site="lax", max_age=60 * 60 * 24 * 7,
                   session_cookie="mmj_session")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

oauth = None
if os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"):
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

if not DEV_MODE and oauth is None:
    raise RuntimeError("로그인 수단 없음. 프로덕션은 GOOGLE_CLIENT_ID/SECRET 필요")

logger.info("부팅: env=%s dev_mode=%s google=%s", APP_ENV, DEV_MODE, oauth is not None)


# --- helpers ---
def _uid(request):
    return request.session.get("uid")


def _require(request):
    uid = _uid(request)
    if not uid:
        raise HTTPException(401, "로그인 필요")
    return uid


def _csrf(request):
    # 상태변경 요청은 커스텀 헤더 요구 (단순 cross-site 폼 POST 차단; SameSite=lax 심층방어)
    if not request.headers.get("x-requested-with"):
        raise HTTPException(403, "요청을 처리할 수 없습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요")


def _login(request, email, name):
    request.session.clear()  # 세션 고정 방지
    request.session["uid"] = db.upsert_user(email, name)
    request.session["email"] = email
    logger.info("login email=%s***", (email or "")[:3])


def _redirect_uri(request):
    return os.getenv("OAUTH_REDIRECT_URI") or str(request.url_for("auth_callback"))


# --- routes ---
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/app" if _uid(request) else "/login")


@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "dev": DEV_MODE, "google": oauth is not None,
        "error": request.query_params.get("error")})


@app.get("/auth/google")
async def auth_google(request: Request):
    if not oauth:
        raise HTTPException(404)
    return await oauth.google.authorize_redirect(request, _redirect_uri(request))


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not oauth:
        raise HTTPException(404)
    try:
        token = await oauth.google.authorize_access_token(request)
        info = token.get("userinfo") or {}
        email = info.get("email")
        if not email:
            raise ValueError("no email")
    except Exception:  # noqa: BLE001
        logger.warning("oauth callback 실패")
        return RedirectResponse("/login?error=oauth")
    _login(request, email, info.get("name") or email)
    return RedirectResponse("/app")


if DEV_MODE:
    @app.get("/dev-login")
    def dev_login(request: Request):
        _login(request, "dev@local", "Dev User")
        return RedirectResponse("/app")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/app", response_class=HTMLResponse)
def app_shell(request: Request):
    if not _uid(request):
        return RedirectResponse("/login")
    with open(os.path.join(HERE, "templates", "app.html"), encoding="utf-8") as f:
        return f.read()


# --- api ---
@app.get("/api/me")
def api_me(request: Request):
    uid = _require(request)
    s = db.get_user_settings(uid)
    return {"email": request.session.get("email", ""), "plan": db.get_user_plan(uid),
            "billing_enabled": BILLING_ENABLED, "support_email": SUPPORT_EMAIL,
            "account_equity": s["account_equity"], "be_pct": s["be_pct"]}


@app.post("/api/settings")
async def api_settings(request: Request):
    uid = _require(request)
    _csrf(request)
    b = await request.json()

    def _num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    eq = _num(b.get("account_equity"))
    be = _num(b.get("be_pct"))
    if eq is not None and eq <= 0:
        eq = None
    if be is not None and not (0 <= be <= 5):  # 본절 밴드 0~5% 합리적 범위
        be = None
    db.set_user_settings(uid, eq, be)
    return {"ok": True}


@app.get("/api/data")
def api_data(request: Request):
    uid = _require(request)
    s = db.get_user_settings(uid)
    be = (s["be_pct"] / 100.0) if s["be_pct"] is not None else analytics.BE_PCT  # 저장은 %, 계산은 분수
    eq = s["account_equity"]
    summary, trades = engine.analyze_user(uid, be)
    return JSONResponse({"summary": summary, "trades": [analytics.enrich(t, eq, be) for t in trades]})


@app.get("/api/market")
def api_market(request: Request):
    _require(request)
    return JSONResponse(market.get_context())


@app.get("/api/positions")
def api_positions(request: Request):
    uid = _require(request)
    try:
        return {"positions": engine.fetch_open_positions(uid)}
    except Exception:  # noqa: BLE001
        logger.exception("positions 실패 uid=%s", uid)
        return JSONResponse({"positions": [], "error": "보유 포지션을 불러오지 못했습니다. 거래소 연결 상태를 확인해 주세요"}, status_code=200)


@app.post("/api/pull")
def api_pull(request: Request):
    uid = _require(request)
    _csrf(request)
    now = time.time()
    wait = PULL_COOLDOWN - (now - _pull_at.get(uid, 0))
    if wait > 0:
        raise HTTPException(429, f"너무 자주 불러왔습니다. {int(wait) + 1}초 뒤에 다시 시도해 주세요")
    _pull_at[uid] = now
    try:
        results = engine.pull_user(uid)  # {exchange: {added, error}} — 거래소별 결과
    except Exception:  # noqa: BLE001
        logger.exception("pull 실패 uid=%s", uid)
        return JSONResponse({"ok": False, "error": "거래를 불러오지 못했습니다. API 키와 read-only 권한을 확인해 주세요"}, status_code=400)
    total = sum(r["added"] for r in results.values())
    return {"ok": True, "fetched": total, "results": results}


@app.post("/api/intent")
async def api_intent(request: Request):
    uid = _require(request)
    _csrf(request)
    b = await request.json()
    def _num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    sl, tp, tp2, tp3 = _num(b.get("sl")), _num(b.get("tp")), _num(b.get("tp2")), _num(b.get("tp3"))
    # 자기채점: 셋업 A/B/C · 실행 A~F · 확신 1~5 (허용값 밖이면 None — 오염 방지)
    setup_grade = b.get("setup_grade") if b.get("setup_grade") in ("A", "B", "C") else None
    exec_grade = b.get("exec_grade") if b.get("exec_grade") in ("A", "B", "C", "D", "F") else None
    conviction = _num(b.get("conviction"))
    conviction = int(conviction) if conviction is not None and 1 <= conviction <= 5 else None
    has_content = (sl is not None or tp is not None or tp2 is not None or tp3 is not None
                   or setup_grade is not None or exec_grade is not None or conviction is not None
                   or any((b.get(k) or "").strip() for k in
                          ("plan", "setup", "strategy", "memo", "emotion", "review", "mistake_tag", "chart_url")))
    reviewed = bool(b.get("reviewed")) and has_content  # 복기완료는 내용이 있어야
    status = "복기완료" if reviewed else ("기록완료" if has_content else "의도 미기입")
    fields = {"plan": b.get("plan"), "setup": b.get("setup"), "strategy": b.get("strategy"),
              "sl": sl, "tp": tp, "tp2": tp2, "tp3": tp3, "emotion": b.get("emotion"), "memo": b.get("memo"),
              "review": b.get("review"), "mistake_tag": b.get("mistake_tag"), "chart_url": b.get("chart_url"),
              "setup_grade": setup_grade, "exec_grade": exec_grade, "conviction": conviction,
              "status": status}
    ok = db.update_intent(uid, b.get("trade_id"), fields)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/api/intent/bulk")
async def api_intent_bulk(request: Request):
    uid = _require(request)
    _csrf(request)
    b = await request.json()
    fields = {"plan": b.get("plan"), "strategy": b.get("strategy"), "emotion": b.get("emotion")}
    return {"ok": True, "filled": db.bulk_fill_unplanned(uid, fields)}


@app.get("/api/connections")
def api_connections(request: Request):
    return {"connected": db.list_connections(_require(request))}


@app.post("/api/connections")
async def api_save_connection(request: Request):
    uid = _require(request)
    _csrf(request)
    b = await request.json()
    kind = b.get("kind")
    warn = None
    if kind in ("bybit", "binance", "gate"):
        key, sec = (b.get("key") or "").strip(), (b.get("secret") or "").strip()
        if len(key) < 8 or len(sec) < 16:
            raise HTTPException(400, "API 키와 시크릿을 다시 확인해 주세요. read-only 키를 권장합니다")
        # Gate는 API로 키 권한을 조회할 수 없어 자동검증 불가 → 사용자 명시 확인 요구(정직성)
        if kind == "gate" and not b.get("ack_readonly"):
            raise HTTPException(400, "Gate는 키 권한 자동 검증이 불가합니다. '읽기 전용 키임을 확인' 체크 후 등록해 주세요")
        # read-only 권한 강제 — 거래/출금 권한 있으면 저장 거부 (Bybit/Binance는 실제 검증)
        try:
            warn = engine.probe_readonly(kind, key, sec).get("warn")
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from None
        except Exception:  # noqa: BLE001
            logger.warning("키 프로빙 실패 kind=%s uid=%s", kind, uid)
            raise HTTPException(400, f"{EXCH_NAMES.get(kind, kind)} 키 권한을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요") from None
        data = {"key": key, "secret": sec}
    elif kind == "notion":
        tok = (b.get("token") or "").strip()
        if not tok:
            raise HTTPException(400, "Notion 토큰을 입력해 주세요")
        data = {"token": tok, "db_id": (b.get("extra") or "").strip()}
    elif kind == "telegram":
        tok = (b.get("token") or "").strip()
        if not tok:
            raise HTTPException(400, "Telegram 봇 토큰을 입력해 주세요")
        data = {"token": tok, "chat_id": (b.get("extra") or "").strip()}
    elif kind == "sheets":
        cr = (b.get("token") or "").strip()
        if not cr:
            raise HTTPException(400, "서비스계정 JSON을 입력해 주세요")
        data = {"creds": cr, "sheet_id": (b.get("extra") or "").strip()}
    else:
        raise HTTPException(400, "지원하지 않는 연동입니다")
    db.set_connection(uid, kind, data)
    return {"ok": True, "warn": warn}


@app.post("/api/connections/delete")
async def api_del_connection(request: Request):
    uid = _require(request)
    _csrf(request)
    db.delete_connection(uid, (await request.json()).get("kind"))
    return {"ok": True}


# 데이터 권리(PIPA/GDPR): 내보내기 + 계정·데이터 완전 삭제
_EXPORT_COLS = ["closed_at", "opened_at", "exchange", "symbol", "direction", "entry", "exit", "qty",
                "pnl", "r", "rr", "risk_usd", "leverage", "fees", "funding", "hold_min", "status",
                "strategy", "setup", "sl", "tp", "tp2", "tp3", "emotion", "plan", "memo",
                "setup_grade", "exec_grade", "conviction",
                "review", "mistake_tag", "chart_url", "exit_reason", "liquidated"]


@app.get("/api/export")
def api_export(request: Request):
    uid = _require(request)
    _, trades = engine.analyze_user(uid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_EXPORT_COLS)
    for t in (analytics.enrich(t) for t in trades):
        w.writerow([analytics.csv_cell(t.get(c)) for c in _EXPORT_COLS])
    body = "﻿" + buf.getvalue()  # BOM — Excel에서 한글 깨짐 방지
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=trading-journal.csv"})


# 결제 스캐폴드 (플래그 뒤 비활성) — Stripe 붙일 준비만. 실제 과금 없음.
@app.post("/api/billing/checkout")
async def api_billing_checkout(request: Request):
    _require(request)
    _csrf(request)
    if not BILLING_ENABLED:
        raise HTTPException(503, "결제는 준비 중입니다. 곧 제공됩니다")
    # TODO(billing): stripe 지연 import + Checkout 세션 생성(STRIPE_SECRET_KEY·PRICE_ID 필요).
    raise HTTPException(503, "결제 설정이 완료되지 않았습니다")


@app.post("/api/account/delete")
async def api_account_delete(request: Request):
    uid = _require(request)
    _csrf(request)
    db.delete_user(uid)  # trades·connections·user 전부 삭제 (되돌릴 수 없음)
    request.session.clear()
    logger.info("account deleted uid=%s", uid)
    return {"ok": True}
