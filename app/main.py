#!/usr/bin/env python3
"""main.py — 매매일지 멀티유저 웹앱 (프로덕션 하드닝).

로컬:  python -m uvicorn app.main:app --port 8000
배포:  uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*" --workers 1
시크릿은 SQLite에 봉투암호화로만 저장. read-only 거래소 키 권장.
"""
import logging
import os
import secrets
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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

from . import db, engine, crypto, market  # noqa: E402

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
    raise RuntimeError("로그인 수단 없음 — 프로덕션은 GOOGLE_CLIENT_ID/SECRET 필요")

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
        raise HTTPException(403, "잘못된 요청")


def _login(request, email, name):
    request.session.clear()  # 세션 고정 방지
    request.session["uid"] = db.upsert_user(email, name)
    request.session["email"] = email
    logger.info("login email=%s***", (email or "")[:3])


def _redirect_uri(request):
    return os.getenv("OAUTH_REDIRECT_URI") or str(request.url_for("auth_callback"))


def _enrich(t):
    """거래에 가격변동%·R·계획RR·리스크·보유시간 파생값 추가."""
    e, x, sl, d, qty = t.get("entry"), t.get("exit"), t.get("sl"), t.get("direction"), t.get("qty")
    tp, tp2 = t.get("tp"), t.get("tp2")
    short = d == "Short"
    if e and x and e != 0:
        t["move_pct"] = round(((x - e) / e * 100) * (-1 if short else 1), 2)
    if e and x and sl and e != sl:
        t["r"] = round((e - x) / (sl - e) if short else (x - e) / (e - sl), 2)
    if e and sl and tp and e != sl:
        t["rr"] = round(abs(tp - e) / abs(e - sl), 2)
    if e and sl and tp2 and e != sl:
        t["rr2"] = round(abs(tp2 - e) / abs(e - sl), 2)
    if e and sl and qty:
        t["risk_usd"] = round(abs(e - sl) * qty, 2)  # 계획 리스크 = 진입~손절 거리 × 수량
    oa, ca = t.get("opened_at"), t.get("closed_at")
    if oa and ca:
        try:
            t["hold_min"] = round((datetime.fromisoformat(ca) - datetime.fromisoformat(oa)).total_seconds() / 60)
        except (TypeError, ValueError):
            pass
    return t


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
    _require(request)
    return {"email": request.session.get("email", "")}


@app.get("/api/data")
def api_data(request: Request):
    uid = _require(request)
    summary, trades = engine.analyze_user(uid)
    return JSONResponse({"summary": summary, "trades": [_enrich(t) for t in trades]})


@app.get("/api/market")
def api_market(request: Request):
    _require(request)
    return JSONResponse(market.get_context())


@app.post("/api/pull")
def api_pull(request: Request):
    uid = _require(request)
    _csrf(request)
    try:
        results = engine.pull_user(uid)  # {exchange: {added, error}} — 거래소별 결과
    except Exception:  # noqa: BLE001
        logger.exception("pull 실패 uid=%s", uid)
        return JSONResponse({"ok": False, "error": "거래 적재 실패 — 키/권한을 확인하세요"}, status_code=400)
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
    sl, tp, tp2 = _num(b.get("sl")), _num(b.get("tp")), _num(b.get("tp2"))
    has_content = (sl is not None or tp is not None or tp2 is not None
                   or any((b.get(k) or "").strip() for k in ("plan", "setup", "strategy", "memo", "emotion")))
    fields = {"plan": b.get("plan"), "setup": b.get("setup"), "strategy": b.get("strategy"),
              "sl": sl, "tp": tp, "tp2": tp2, "emotion": b.get("emotion"), "memo": b.get("memo"),
              "status": "기록완료" if has_content else "의도 미기입"}
    ok = db.update_intent(uid, b.get("trade_id"), fields)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


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
            raise HTTPException(400, "유효한 API 키/시크릿이 필요합니다 (read-only 권장)")
        # read-only 권한 강제 — 거래/출금 권한 있으면 저장 거부
        try:
            warn = engine.probe_readonly(kind, key, sec).get("warn")
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from None
        except Exception:  # noqa: BLE001
            logger.warning("키 프로빙 실패 kind=%s uid=%s", kind, uid)
            raise HTTPException(400, f"{kind} 키 권한 확인 실패 — 잠시 후 다시 시도하세요") from None
        data = {"key": key, "secret": sec}
    elif kind == "notion":
        tok = (b.get("token") or "").strip()
        if not tok:
            raise HTTPException(400, "Notion 토큰 필요")
        data = {"token": tok, "db_id": (b.get("extra") or "").strip()}
    elif kind == "telegram":
        tok = (b.get("token") or "").strip()
        if not tok:
            raise HTTPException(400, "Telegram 봇 토큰 필요")
        data = {"token": tok, "chat_id": (b.get("extra") or "").strip()}
    elif kind == "sheets":
        cr = (b.get("token") or "").strip()
        if not cr:
            raise HTTPException(400, "서비스계정 JSON 필요")
        data = {"creds": cr, "sheet_id": (b.get("extra") or "").strip()}
    else:
        raise HTTPException(400, "unknown kind")
    db.set_connection(uid, kind, data)
    return {"ok": True, "warn": warn}


@app.post("/api/connections/delete")
async def api_del_connection(request: Request):
    uid = _require(request)
    _csrf(request)
    db.delete_connection(uid, (await request.json()).get("kind"))
    return {"ok": True}
