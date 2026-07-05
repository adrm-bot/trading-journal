#!/usr/bin/env python3
"""REGIME_PLAYBOOK: state -> what to run / what to TURN OFF.

Pure lookup, deliberately isolated: the classifier never imports this module, and this module
never imports the classifier. Gating is mostly about the `avoid` list.
"""

REGIME_PLAYBOOK = {
    "TREND_UP": {
        "primary": "추세추종 롱 (눌림목 연속 진입, HT1 계열)",
        "secondary": ["돌파 연속 진입", "모멘텀 스캘프 롱"],
        "avoid": ["역추세 페이드", "평균회귀 숏", "레인지 페이드"],
    },
    "TREND_DOWN": {
        "primary": "추세추종 숏 (반등 되돌림 진입)",
        "secondary": ["붕괴 연속 진입", "모멘텀 스캘프 숏"],
        "avoid": ["역추세 페이드", "평균회귀 롱", "나이프 캐칭"],
    },
    "SQUEEZE": {
        "primary": "돌파 대기 (레벨 스트래들, 확장 확인 후 진입)",
        "secondary": ["돌파 레벨 알림 세팅", "포지션 축소 후 관망"],
        "avoid": ["추세추종 진입 (아직 추세 없음)", "미시 등락 추격"],
    },
    "RANGE": {
        "primary": "레인지 극단 평균회귀",
        "secondary": ["지지/저항 반등 스캘프"],
        "avoid": ["추세추종", "돌파 추격 (거짓돌파 서식지)"],
    },
    "CHOP": {
        "primary": "관망 / 리스크 오프",
        "secondary": ["사이즈 축소", "알림만 유지"],
        "avoid": ["방향성 진입 전부", "물타기/불타기", "복수 매매"],
    },
}


def validate() -> None:
    from classifier import REGIMES  # test-time check only; classifier never imports us
    assert set(REGIME_PLAYBOOK) == set(REGIMES)
    for state, entry in REGIME_PLAYBOOK.items():
        assert entry["primary"], state
        assert isinstance(entry["secondary"], list), state
        assert entry["avoid"], f"{state}: avoid is load-bearing, must be non-empty"
