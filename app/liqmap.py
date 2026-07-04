"""liqmap.py — 예측형 청산맵(코인글래스/KingFisher식 '자석 구간') 유료 제공자 연동 스캐폴드.

liquidations.py(실제 청산 체결·무료)와 다르다: 이건 미결제약정·레버리지로 만든 '앞으로 어디서
청산이 몰릴까' 예측 맵으로, 무료 API가 없다(KingFisher Pro $79+/월, 코인글래스 $29+/월).

현재 상태: **키 저장 + 실제 호출 배관까지만.** KingFisher 응답 스키마는 유료라 미검증 →
받은 원본(raw)만 돌려주고, 시각화 매핑은 실 키로 검증한 뒤 활성(베타). 지어내지 않는다.
엔드포인트/헤더는 thekingfisher.io 공개 문서 기준(POST /api/map/latest, X-API-Key).
"""
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("app.liqmap")

KF_URL = "https://app.thekingfisher.io/api/map/latest"
_TIMEOUT = 12


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
