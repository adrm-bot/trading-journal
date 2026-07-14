"""liqmap.py — 예측형 청산맵(코인글래스/KingFisher식 '자석 구간') 유료 제공자 연동 스캐폴드.

liquidations.py(실제 청산 체결·무료)와 다르다: 이건 미결제약정·레버리지로 만든 '앞으로 어디서
청산이 몰릴까' 예측 맵으로, 무료 API가 없다. 가격·허용 심볼·재배포 조건은 공급자의 최신
구독 페이지와 계약을 확인한다(고정 가격을 코드 설명에 남기지 않는다).

현재 상태: **키 저장 + 실제 호출 배관까지만.** KingFisher 응답 스키마는 유료라 미검증 →
받은 원본(raw)만 돌려주고, 시각화 매핑은 실 키로 검증한 뒤 활성(베타). 지어내지 않는다.
엔드포인트/헤더는 thekingfisher.io 공개 문서 기준(POST /api/map/latest, X-API-Key).
"""
import json
import logging
import re
import urllib.error
import urllib.request

logger = logging.getLogger("app.liqmap")

KF_URL = "https://app.thekingfisher.io/api/map/latest"
_TIMEOUT = 12
PROVIDERS = ("kingfisher", "coinglass")
PROVIDER_NAMES = {"kingfisher": "KingFisher", "coinglass": "CoinGlass"}
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
MAX_SYMBOLS = 20


def normalize_symbol(value):
    """공급자와 무관한 내부 심볼 규격으로 정리한다.

    입력 편의를 위해 BTC, btc/usdt, BTC-USDT:USDT를 모두 BTCUSDT로 받는다.
    실제 제공자별 pair 형식 변환은 API 어댑터에서 처리한다.
    """
    raw = str(value or "").strip().upper()
    if not raw:
        raise ValueError("추적할 심볼을 입력해 주세요")
    raw = raw.split(":", 1)[0]
    raw = re.sub(r"[\s/_-]+", "", raw)
    if raw.endswith("PERP"):
        raw = raw[:-4]
    if not re.fullmatch(r"[A-Z0-9]{2,20}", raw):
        raise ValueError(f"심볼 형식을 확인해 주세요: {value}")
    if not raw.endswith(("USDT", "USDC", "USD")):
        raw += "USDT"
    if len(raw) > 20:
        raise ValueError(f"심볼이 너무 깁니다: {value}")
    return raw


def normalize_watchlist(values):
    """순서를 보존해 중복을 제거하고 공급자 공용 한도를 적용한다."""
    if not isinstance(values, (list, tuple)):
        raise ValueError("심볼 목록 형식이 올바르지 않습니다")
    out = []
    for value in values:
        symbol = normalize_symbol(value)
        if symbol not in out:
            out.append(symbol)
    if not out:
        raise ValueError("추적 심볼을 1개 이상 등록해 주세요")
    if len(out) > MAX_SYMBOLS:
        raise ValueError(f"공급자별 추적 심볼은 최대 {MAX_SYMBOLS}개입니다")
    return out


def fetch_kingfisher(key, pair="BTCUSDT", exchange="binance", kind="perpetual"):
    """KingFisher 최신 청산맵 1건. 키 없으면/실패면 available:false(대시보드 안 죽임).
    반환 raw는 원본 JSON — 스키마 매핑(시각화)은 실 키 검증 후."""
    if not key:
        return {"available": False, "connected": False, "reason": "KingFisher API 키 미연결"}
    payload = json.dumps({"pair": pair, "exchange": exchange, "type": kind}).encode()
    req = urllib.request.Request(KF_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 (고정 https)
            raw = json.loads(r.read().decode())
        # 스키마 미검증 — 원본 그대로 반환(프론트는 '응답 수신 · 시각화 베타'로 표시)
        return {"available": True, "connected": True, "source": "kingfisher",
                "pair": pair, "exchange": exchange, "raw": raw}
    except urllib.error.HTTPError as e:
        reason = "API 키가 거부되었거나 크레딧/구독이 없습니다" if e.code in (401, 403, 402) \
            else f"KingFisher 오류(HTTP {e.code})"
        return {"available": False, "connected": True, "reason": reason}
    except Exception:  # noqa: BLE001 — 타임아웃·네트워크·파싱
        logger.warning("liqmap: KingFisher 조회 실패 pair=%s", pair)
        return {"available": False, "connected": True, "reason": "KingFisher 응답을 받지 못했습니다"}


def fetch_coinglass(key, pair="BTCUSDT"):
    """CoinGlass 유료 API 자리.

    실 키와 구독 플랜별 응답을 확인하기 전에는 엔드포인트를 추정해 호출하지 않는다. 키와
    워치리스트는 미리 저장할 수 있고, 어댑터가 연결되기 전까지 정직한 준비 상태만 반환한다.
    """
    if not key:
        return {"available": False, "connected": False, "reason": "CoinGlass API 키 미연결"}
    return {"available": False, "connected": True, "source": "coinglass", "pair": pair,
            "reason": "CoinGlass API 어댑터 준비됨 · 실 키로 응답 스키마 확인 후 활성화"}


def fetch(provider, key, pair="BTCUSDT"):
    """공급자 선택을 한 곳에서 검증하는 예측형 청산맵 진입점."""
    if provider == "kingfisher":
        return fetch_kingfisher(key, pair)
    if provider == "coinglass":
        return fetch_coinglass(key, pair)
    raise ValueError("지원하지 않는 청산맵 공급자입니다")
