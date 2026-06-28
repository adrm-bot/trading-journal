"""crypto.py — 저장 시크릿(거래소 키 등) 봉투암호화 (Fernet/AES)."""
import os
from cryptography.fernet import Fernet


def _fernet():
    key = os.getenv("APP_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("APP_SECRET_KEY 미설정 — `python -m app.genkey` 로 생성해 환경변수에 넣어라.")
    return Fernet(key.encode())


def ensure_configured():
    """부팅 시 1회 호출 — 키가 유효한 Fernet 키인지 즉시 검증(fail-fast)."""
    _fernet().encrypt(b"ok")


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
