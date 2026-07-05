#!/usr/bin/env python3
"""REGIME_PLAYBOOK: state -> what to run / what to TURN OFF.

Pure lookup, deliberately isolated: the classifier never imports this module, and this module
never imports the classifier. Gating is mostly about the `avoid` list.
"""

REGIME_PLAYBOOK = {
    "TREND_UP": {
        "primary": "추세추종 롱 — 눌림목마다 나눠서 진입하는 방식이 기본입니다 (HT1 계열)",
        "secondary": ["돌파 이후 연속 진입", "모멘텀 스캘프 롱"],
        "avoid": ["추세를 거스르는 페이드", "평균회귀 숏", "레인지 페이드"],
    },
    "TREND_DOWN": {
        "primary": "추세추종 숏 — 반등 되돌림에서 진입하는 방식이 기본입니다",
        "secondary": ["붕괴 이후 연속 진입", "모멘텀 스캘프 숏"],
        "avoid": ["추세를 거스르는 페이드", "평균회귀 롱", "떨어지는 칼날 잡기"],
    },
    # 143k 백테스트 생존 통계 인사이트 반영(2026-07-05): 돌파는 최약체 생존군(19/196, 휩소)
    # → 첫 돌파 추격 금지·확장 확인 후 진입. 평균회귀는 거래량 동반 시 신뢰도 최고(BB+Vol 67%).
    "SQUEEZE": {
        "primary": "돌파를 기다리세요 — 변동성 확장이 확인된 뒤에 진입하고, 첫 돌파는 쫓지 마세요",
        "secondary": ["돌파 예상 레벨에 알림 설정", "포지션을 줄이고 관망"],
        "avoid": ["첫 돌파 추격 (백테스트에서 생존율이 가장 낮았던 전략군입니다)",
                  "방향을 미리 정해두는 베팅", "미시 등락 추격"],
    },
    "RANGE": {
        "primary": "박스 극단에서의 평균회귀 — 거래량이 함께 실린 이탈과 회귀만 노리세요",
        "secondary": ["지지·저항 반등 스캘프"],
        "avoid": ["추세추종", "돌파 추격 (박스권은 거짓 돌파가 잦습니다)"],
    },
    "CHOP": {
        "primary": "관망이 기본입니다 — 리스크를 줄이세요",
        "secondary": ["사이즈 축소", "알림만 유지"],
        "avoid": ["방향성 진입 전부", "물타기와 불타기", "손실 복구용 복수 매매"],
    },
}


def validate() -> None:
    from classifier import REGIMES  # test-time check only; classifier never imports us
    assert set(REGIME_PLAYBOOK) == set(REGIMES)
    for state, entry in REGIME_PLAYBOOK.items():
        assert entry["primary"], state
        assert isinstance(entry["secondary"], list), state
        assert entry["avoid"], f"{state}: avoid is load-bearing, must be non-empty"
