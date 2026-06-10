# Auto-trade-system — HT1 v2

크립토 USDT 무기한선물용 **추세추종 눌림목 진입 엔진** (TradingView Pine v6) + Claude 연계 자동매매 로드맵.

v1(HT1_By_Gunner)의 정밀 감사에서 발견된 구조 결함(소급 진입가, R 과대평가, 리페인트 등 20+건)을 전면 재설계로 해소한 버전. 설계 근거와 수식은 [docs/HT1_v2_SPEC.md](docs/HT1_v2_SPEC.md) 참조.

## 파일 구성

| 파일 | 용도 |
|---|---|
| [pine/HT1_v2_indicator.pine](pine/HT1_v2_indicator.pine) | **알림·실운용 지표** — 상태기계 엔진 + 성과 집계 + JSON 알림 |
| [pine/HT1_v2_strategy.pine](pine/HT1_v2_strategy.pine) | **백테스트 전략** — 동일 엔진 + TV 전략 테스터 집행 |
| [pine/HT1_v1_backup.pine](pine/HT1_v1_backup.pine) | v1 원본 보존 (비교 기준) |
| [docs/HT1_v2_SPEC.md](docs/HT1_v2_SPEC.md) | 엔진 명세 — 상태기계·청산 정책·시간 기준·v1 감사 매핑 |
| [docs/alert_webhook_schema.json](docs/alert_webhook_schema.json) | 알림 JSON **단일 정본** (서버·필터·집행의 계약) |
| [docs/CLAUDE_INTEGRATION.md](docs/CLAUDE_INTEGRATION.md) | TV↔Claude 연계 아키텍처 + Phase A~D 로드맵 + ADR |

## v2 핵심 (v1과 다른 점)

- **시점 일원화**: 진입가·라벨·알림·통계 전부 시그널 확정 봉 기준. v1은 MACD 크로스 봉 종가를 최대 10봉 소급 표기 → 체결 불가능한 가격이 차트·알림에 노출됐었음.
- **풀백 상태기계**: IDLE→IMPULSE→PULLBACK→COOLDOWN. 되돌림 깊이(피보 존)·지속 봉수·거래량 수축·RSI 존을 측정하는 "눌림목 품질 게이트". v1의 "최근 30봉 내 EMA 터치"는 현재 풀백과 무관한 과거 터치로도 통과되는 구조였음.
- **트리거 3모드**: 눌림 고점 돌파(기본) / 패턴 추세선 돌파(웻지·삼각수렴·플래그) / MACD 크로스(레거시). MACD 크로스는 풀백 저점 후 3~7봉 지연 + whipsaw가 약점 — 전략 버전으로 A/B 비교 후 확정.
- **ADX 위상 보정**: ADX·DI·ER을 진입 시점이 아닌 **임펄스→풀백 전이 시점에 캡처** (풀백 중 ADX가 하락해 좋은 진입을 거부하는 위상역전 회피). 백분위 정규화 + ADX 40+ 고점 후 꺾임 = 추세 소진 감점.
- **실행 가능한 청산 정책**: TP1(1.5R) 50% 청산 + SL→BE + 잔여 TP2(3R)/BE. 성과 집계가 이 정책의 실현 R만 기장 (갭 체결·수수료·만료 MTM 반영). v1은 TP1 후 SL 미추적으로 R을 구조적으로 과대평가했음.
- **시그널 품질 스코어 0~100 + 등급(A/B/C)** + 등급 사이징, 패턴 가점, 멀티심볼 이식 가능한 무차원 파라미터.

## TradingView 적용 방법

1. TradingView → Pine Editor → 새 지표 → `pine/HT1_v2_indicator.pine` 내용 전체 붙여넣기 → 저장·차트에 추가.
2. 권장 차트: BTCUSDT.P / ETHUSDT.P (Bybit·Binance), **15m 또는 1h** (1~5m 스캘핑은 프리셋 `보수` + 모호율 경고 확인).
3. 설정은 **프리셋(보수/표준/공격)** 으로만 조정 — 개별 임계값은 과최적화 방지를 위해 동결됨.
4. 전략 테스터: `pine/HT1_v2_strategy.pine`을 새 전략으로 붙여넣기. **Premium 필요** (`use_bar_magnifier`) — 하위 플랜이면 선언부에서 해당 인자 삭제.

### 알림 설정 (웹훅 대비 JSON)

1. 차트에 지표 추가 → 알림 생성 → 조건: **"HT1v2"** → "Any alert() function call".
2. 메시지 칸은 비워둠 (JSON은 코드가 생성 — `{{...}}` placeholder 불필요).
3. 만료: Premium은 무기한(open-ended) 선택.
4. 멀티심볼 운용: 같은 지표를 심볼별 차트에 추가하고 알림을 각각 생성 (Premium 알림 한도 800개). 파라미터가 무차원이라 동일 설정으로 BTC/ETH/SOL 등 이식 가능 — 시그널 빈도를 늘리는 가장 안전한 방법.
5. 페이로드 형식 확인: webhook URL에 https://webhook.site 임시 주소를 넣고 수신 JSON이 [docs/alert_webhook_schema.json](docs/alert_webhook_schema.json)과 일치하는지 검증.

## 검증 절차 (SPEC §15)

| 단계 | 방법 | 통과 기준 |
|---|---|---|
| 1. 컴파일 | 두 .pine 파일을 Pine Editor에 붙여넣기 | 에러 0 |
| 2. 시그널 정합 | 지표와 전략을 같은 차트에 올려 시그널 봉 비교 | 동일 봉에서 발생 |
| 3. 체결 모델 분리 | 전략 Properties → "Fill orders on bar close" ON으로 1회, OFF로 1회 실행 | ON↔지표 E[R] 차이 = 모호성 처리 차이 / ON↔OFF 차이 = 지연 비용 |
| 4. 트리거 A/B/C | 트리거 모드 3종 × BTC·ETH·SOL·BNB·XRP × 15m/1h | **1차: MAR(연환산수익÷최대DD)·R/일**, 2차: E[R]·승률·빈도 |
| 5. 강건성 | 프리셋 3종 성과가 급변하지 않는지 (plateau) | 이웃 설정 성과 안정 |

테이블의 **모호율 >10%** 경고는 TF가 너무 크다는 신호, **만료율 >30%** 는 평가 기간 부족 신호.

## 코어 동결 섹션 동기화 검증

두 .pine 파일의 `// ==== HT1-CORE v2.0.0 BEGIN/END ====` 구간은 텍스트 동일해야 한다. 수정 후 검증:

```powershell
$a=[regex]::Match((Get-Content pine\HT1_v2_indicator.pine -Raw -Encoding UTF8),'(?s)// ==== HT1-CORE.*?END ====').Value
$b=[regex]::Match((Get-Content pine\HT1_v2_strategy.pine  -Raw -Encoding UTF8),'(?s)// ==== HT1-CORE.*?END ====').Value
if($a -eq $b){"CORE OK"}else{"CORE MISMATCH"}
```

## 다음 단계

[docs/CLAUDE_INTEGRATION.md](docs/CLAUDE_INTEGRATION.md)의 Phase B(웹훅 수신 + Claude veto PoC, 1주·주문 없음)부터. 자동집행(Phase C)은 반자동 4주+ 운영 데이터로 전환 조건 충족 후.
