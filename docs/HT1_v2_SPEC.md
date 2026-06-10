# HT1 v2 엔진 명세서 (SPEC)

> 이 문서가 `pine/HT1_v2_indicator.pine` / `pine/HT1_v2_strategy.pine` / (향후) 서버의 **단일 기준**이다.
> 알림 페이로드는 [alert_webhook_schema.json](alert_webhook_schema.json)이 정본이며 이 문서는 그것을 참조만 한다.
> 엔진 버전: **2.0.0** (2026-06)

---

## 0. 헌법 2조

1. **시점 일원화** — 구조 정보(임펄스 고저점, 풀백 깊이, ER/ADX)는 상태기계 `var`로 전이 시점에 캡처한다. 진입가·라벨·알림·통계 등록은 전부 **시그널 확정 봉**에서 평가한다 (`entry = close`, `barstate.isconfirmed` 강제). `close[offset]` 류 동적 인덱싱, 과거 봉 소급 라벨, 소급 가격 송출은 전면 금지. (v1 감사 C-1/H-4/H-6/M-7 해소)
2. **무차원화** — 모든 임계값은 ATR 배수·백분위·비율로 정규화한다. 절대 가격 단위 임계 금지. (M-2 해소, 멀티심볼·멀티TF 이식성)

## 1. 시간 기준 명세표

| 항목 | 평가/캡처 시점 | 비고 |
|---|---|---|
| 레짐 게이트 (slope_norm, EMA 정렬) | 시그널 봉 (매봉 갱신) | 시그널 시점에 추세 유효해야 함 |
| ER / ADX백분위 / DI 정렬 / ADX 소진 | **임펄스→풀백 전이 봉에 1회 캡처** | 풀백 중 하락하는 위상 왜곡 회피 |
| impHigh/impLow, 풀백 깊이·봉수·거래량·RSI저점·패턴 | 상태기계 `var` (풀백 중 누적) | 시그널 봉 도달 시 이미 확정값 |
| 트리거·진입가·SL/TP·라벨·알림·통계 등록 | **시그널 확정 봉** | `entry = close`, 라벨도 이 봉에만 |
| HTF 추세 | 직전 **확정** HTF 봉 (`[1] + lookahead_on`) | 실시간 리페인트 차단 (H-3) |

## 2. 레짐 판정

```
ema_f = EMA(close, 50),  ema_s = EMA(close, 200)        // EMA 100 제거 (자유도 절감)
slope_norm = (ema_s − ema_s[20]) / (ATR(14) × 20)        // 무차원 기울기
regimeBull = ema_f > ema_s AND slope_norm > +0.05        // 프리셋: 보수 0.08 / 표준 0.05 / 공격 0.03
regimeBear = ema_f < ema_s AND slope_norm < −0.05
deadMarket = percentrank(ATR/close, 200) < 10            // 죽은 장 제외 (게이트)
```

**추세 강도 확인** — enum `{ER(기본), ADX, 둘 다}`, **임펄스→풀백 전이 시점에 1회 캡처**:
- `cap_er = |close − close[20]| / Σ|close − close[1]|(20)` ≥ 0.30 (보수 0.35 / 공격 0.25)
- ADX 모드: `cap_adx_pct = percentrank(ADX(14), 200)` ≥ 50 **AND** DI 정렬(롱: +DI > −DI) — 고정 임계 25 대신 자기 정규화 (M-5 해소)
- **ADX 소진 감지**: 임펄스 구간 중 ADX 최고값 ≥ 40 이고 전이 시점 ADX < 최고값 × 0.9 → `adx_exhaust = true` (게이트 아님, 스코어 −15)

## 3. 풀백 상태기계 (방향별 독립 2개: 롱 SM / 숏 SM 대칭)

```
Phase: IDLE → IMPULSE → PULLBACK → (시그널) → COOLDOWN → IDLE
```

**전이 규칙 (롱 SM 기준, 숏은 완전 대칭):**

| 전이 | 조건 | 액션 |
|---|---|---|
| IDLE→IMPULSE | regimeBull AND close == highest(close, 20) | impLow := 직전 확정 피벗로우(pivotlow 3/3, 없으면 lowest(low,20)); impHigh := high; ADX피크 추적 시작 |
| IMPULSE 중 | — | impHigh := max(impHigh, high); 임펄스 거래량 누적; ADX피크 := max(...); regimeBull 붕괴 시 → IDLE |
| IMPULSE→PULLBACK | impHigh − low ≥ 1.0 × ATR | **cap_er/cap_adx_pct/DI/adx_exhaust 캡처**; pbStart := bar_index; pbLow := low; pbMinorHigh := high; 풀백 거래량·RSI저점 추적 시작; 패턴 수집 시작 |
| PULLBACK 중 | — | pbLow := min(pbLow, low) (갱신 봉에서 pbMinorHigh := high 리셋); pbLow 미갱신 봉에선 pbMinorHigh := max(pbMinorHigh, high) |
| PULLBACK→IMPULSE | high > impHigh (신고가 = 풀백 무산, 임펄스 연장) | 풀백 추적 리셋 |
| PULLBACK→IDLE (무효화) | close < impLow OR pb_bars > 15 OR depth > 0.78 | 전부 리셋 |
| PULLBACK→COOLDOWN | **시그널 발생** | 시그널 처리 |
| COOLDOWN→IDLE | 10봉 경과 (상수) | — |

**동일 봉 처리 순서 (고정, 재현성 계약):** ① 상태 갱신(impHigh/pbLow/pbMinorHigh/거래량/RSI/패턴 수집) → ② 무효화·복귀 체크 → ③ 트리거 평가(이때 pbMinorHigh는 **①에서 당봉 high가 반영되기 전의 값** = `pbMinorHigh_prev` 사용 — 자기참조 차단, 비평 #3) → ④ 시그널 시 COOLDOWN 전이.

**품질 게이트 (시그널 전제, 풀백 누적값으로 평가):**

| 게이트 | 보수 | **표준** | 공격 |
|---|---|---|---|
| depth = (impHigh−pbLow)/(impHigh−impLow) | 0.38–0.62 | **0.30–0.70** | 0.25–0.78 |
| pb_bars | 4–12 | **3–15** | 2–20 |
| vol_pb_avg / vol_imp_avg < | 0.7 | **0.8** | 0.9 |
| 풀백 중 RSI(14) 최저값 ∈ | 40–55 | **35–55** | 30–58 |

## 4. 패턴 분류기 (풀백 형태)

PULLBACK 구간(n ≥ 5봉)의 고가·저가 각각에 OLS 직선 적합 (피벗 부족 문제 회피 — 인트라데이 풀백은 3~15봉이라 피벗 3/3로는 표본 부족):

```
slope_hi, slope_lo  : 봉당 기울기 / ATR (무차원)
width_start, width_now : 적합선 간 폭 (풀백 시작 시점 vs 현재)
contract = width_now / width_start
fit_ok   : 양쪽 R² ≥ 0.5 (노이즈 오분류 차단)
```

**분류 (롱 컨텍스트 — 추세 지속 패턴만, 역추세 반전 사용 금지):**

| 패턴 | 조건 (상수 동결) |
|---|---|
| falling_wedge | slope_hi < −0.02 AND slope_lo < −0.02 AND slope_hi < slope_lo AND contract < 0.7 |
| flag | slope_hi < −0.02 AND slope_lo < −0.02 AND \|slope_hi − slope_lo\| ≤ 0.03 (평행 하락 채널) |
| triangle | slope_hi < −0.02 AND slope_lo > +0.02 AND contract < 0.7 |

숏 컨텍스트는 rising_wedge/flag/triangle 대칭. 활용: (a) 스코어 +15, (b) 트리거 모드 ② 돌파 레벨 = 상단 적합선의 당봉 투사값(적합은 **전봉까지의 데이터**로 산출), (c) 패턴 추세선 시각화. Bulkowski 지속 패턴 통계는 사전확률일 뿐 — 유효성은 전략 A/B/C로 직접 검증.

## 5. 트리거 (enum 3모드)

| 모드 | 조건 (롱) | 비고 |
|---|---|---|
| ① breakout (기본) | close > pbMinorHigh_prev AND histTurn | histTurn = hist > hist[1] AND hist[1] ≤ hist[2] (MACD 12/26/9) |
| ② pattern | 패턴 검출 시: close > 상단 적합선 투사값 AND histTurn / 미검출 시 ①로 폴백 | |
| ③ macd (레거시) | goldenCross (PULLBACK 중) | 상태기계가 게이트하므로 v1의 윈도우·클러스터 불필요 (C-3 자연 소멸) |

stop-entry 선설치(돌파 레벨 사전 주문)는 **별도 백테스트 통과 전 금지** — 종가 확인 돌파와 다른 전략임 (비평 #3).

## 6. 최종 시그널 (롱)

```
longSignal = PhaseL == PULLBACK            // ①② 처리 후에도 유효
         AND regimeBull AND NOT deadMarket
         AND 품질 게이트 4종 통과
         AND 강도 확인 (cap_er / cap_adx_pct+DI, 모드별)
         AND 트리거 (모드별)
         AND htfBull (HTF 확정봉)
         AND valid_risk                     // §7
         AND funding_ok AND session_ok AND vol_circuit_ok   // §10
         AND direction 허용
         AND barstate.isconfirmed           // 항상 강제 (토글 아님)
```

## 7. SL / TP / 리스크

```
entry = close                               // 시그널 봉 종가, mintick 라운딩
sl    = pbLow − 0.5 × ATR                   // 구조 SL 기본 (EMA 모드는 enum 강등)
risk  = entry − sl
valid_risk = 0.5×ATR ≤ risk ≤ 3.0×ATR       // 위반 시 시그널 폐기 (SL 확장 금지 — 손익비 은닉 훼손 방지)
tp1 = entry + 1.5 × risk
tp2 = entry + 3.0 × risk                    // enum: fixed(기본)/구조(max(스윙하이, entry+2R))/샹들리에
BE  = entry + 0.1 × ATR                     // 수수료 보전 오프셋 — 단일 정의 (비평 #8 해소)
```

## 8. 청산 정책 상태기계 (지표 집계·전략·서버 공통 단일 정책)

**정책: TP1에서 50% 청산 → SL을 BE로 이동 → 잔여 50%는 TP2 또는 BE.**

```
OPEN ──(low ≤ sl)──────────→ LOSS:    R = −(entry − fill)/risk − cost_r,  fill = min(sl, open)   ← 갭 체결
     ├─(high ≥ tp1)────────→ PARTIAL: realized += 0.5×1.5R;  sl := BE
     ├─(타임스톱: N봉 내 high < entry+1R, 옵션)→ TIME_OUT: R = (close−entry)/risk − cost_r
     └─(eval_bars=60 경과)─→ EXPIRED: R = (close−entry)/risk − cost_r                            ← MTM, 0R 금지

PARTIAL ──(low ≤ BE)───────→ BE_OUT:  R = 0.75 + 0.5×(fill_be−entry)/risk − cost_r ≈ +0.75R
        ├─(high ≥ tp2)─────→ FULL:    R = 0.75 + 0.5×3.0 − cost_r = +2.25R − cost_r
        └─(eval_bars 경과)─→ EXPIRED: R = 0.75 + 0.5×(close−entry)/risk − cost_r

cost_r = 2 × fee_pct × entry / risk          // 테이커 왕복, 기본 fee 0.055%
동시 터치(같은 봉 low≤sl AND high≥tp): SL 우선 기장(보수) + ambiguous 카운트 별도 집계
  → 테이블에 모호율 표기, >10%면 TF 과대 신호. 전략 버전의 Bar Magnifier가 상한 검증.
트레이드 ID = 시그널 봉 bar_index (중복 등록 차단, H-5 해소 — v2는 크로스 봉 개념 없음)
```

집계 표기: E[R], 승률, **만료율**(>30%면 eval_bars 부족), **모호율**, BE_OUT 비율, "표시 라벨 N / 누적 N" (L-4).

## 9. 사이징

```
risk_amt    = acct_size × risk_pct(0.5%) / 100
qty_raw     = risk_amt / risk
grade 사이징(옵션): A=1.0×, B=0.75×
implied_lev = qty × entry / acct_size ≤ max_lev(10) — 초과 시 절삭 + 경고
```
qty_hint는 **참고치** — 최종 권위는 서버 (실잔고 + qtyStep/minOrderQty + 추정 청산가 vs SL 검증). USDT **선형 무기한 한정** (인버스·분기물·CME 범위 외 — 비평 #9).

## 10. 선물 필터

- **펀딩 회피**: `time_close` 기준 (봉 시작 아님 — 비평 #9), 펀딩 주기 입력(기본 8h, 1h/4h 심볼 대응), 펀딩 전 10분 진입 금지
- **주말 차단** (옵션, 기본 OFF — 크립토 24h)
- **변동성 서킷브레이커**: ATR(14)/ATR(100) ≥ 2.5 → 신규 진입 중지

## 11. 스코어 (0–100, 등급화 전용 — 게이트 아님)

| 구성 | 가중치 | 산식 (0~1 정규화) |
|---|---|---|
| 레짐 강도 | 20 | min(slope_norm/0.15, 1)×0.5 + min(강도확인값 정규화, 1)×0.5 |
| 깊이 적합도 | 20 | 1 − \|depth − 0.5\| / 0.28 (50% 중심 삼각형) |
| 거래량 수축 | 15 | min((1 − vol_ratio)/0.4, 1) |
| 모멘텀 동의 | 15 | histTurn +0.5, hist < 0(눌림 영역) +0.5 |
| MTF 정합 | 10 | htf 일치 = 1 |
| 캔들 확인 | 5 | 시그널 봉 장악형/핀바 = 1 |
| 패턴 | 15 | 패턴 검출 = 1 |
| ADX 소진 감점 | −15 | adx_exhaust 시 |

등급: **A ≥ 80, B 65–79, C < 65**. 라벨 `"L 78 A"`. 알림: 최소 등급 input (수동 운용 기본 B; 서버 도입 후 전량 송출 + 서버 라우팅으로 전환해 C등급 섀도우 평가 — 비평 #7).

## 12. MTF

```
htf_ema = request.security(tickerid, htf, EMA(close,200)[1], lookahead_on)   // 확정봉 고정 패턴
htf_cls = request.security(tickerid, htf, close[1],          lookahead_on)
htfBull = htf_cls > htf_ema
HTF 선택: 자동(차트 TF × 4, timeframe.from_seconds) / 수동. 차트 TF ≥ HTF면 runtime.error.
```

## 13. 파라미터 (노출 동결 — L-1 해소)

노출: preset(보수/표준/공격) · trig_mode(3) · 강도확인 모드(3) · htf(자동/수동) · min_alert_grade · direction(롱/숏/양방) · fee_pct · acct_size · risk_pct · max_lev · grade_sizing(토글) · time_stop(토글) · 펀딩 필터 2 · 표시 토글류(로직 무관). **로직 임계값은 전부 프리셋/상수** — v1의 bypass류 토글 전면 삭제. EMA 50/200·RSI 14·ATR 14·MACD 12/26/9·RR 1.5/3.0·SL버퍼 0.5·BE 0.1·쿨다운 10봉·eval 60봉 = 상수.

## 14. v1 감사 발견 → v2 해소 매핑

| v1 발견 | v2 해소 |
|---|---|
| C-1 소급 진입가 | §0-1, §1 — 시그널 봉 일원화, 동적 인덱싱 금지 |
| C-2 R 과대평가 | §8 — 명시적 청산 정책 상태기계, 갭 체결, MTM 만료 |
| C-3 클러스터 dedup | §5 — 상태기계 도입으로 개념 자체 소멸 |
| H-1 동시터치/갭/수수료 | §8 — SL 우선+모호율, fill=min(sl,open), cost_r |
| H-2 만료 0R | §8 — EXPIRED = MTM |
| H-3 HTF 리페인트 | §12 — [1]+lookahead_on |
| H-4 valuewhen/na | §0-1 — var 캡처 패턴 |
| H-5 쿨다운/ID 불일치 | §8 — 트레이드 ID = 시그널 봉 (단일 시간 기준) |
| H-6 알림 소급가 | alert() JSON 단일 경로, [정본 스키마](alert_webhook_schema.json) |
| M-1 터치 순서 미강제 | §3 — IMPULSE→PULLBACK→트리거 순서 강제 |
| M-2 비정규화 기울기 | §2 — slope_norm |
| M-3 풀백 질 무측정 | §3 게이트 + §4 패턴 |
| M-4 강도 플라시보 | histTurn 가속 조건으로 대체 |
| M-5 ADX 고정 임계 | §2 — 백분위 + 임펄스 캡처 + DI + 소진 감지 |
| M-6 risk 가드 부재 | §7 — valid_risk, 폐기 원칙 |
| M-7 시점 혼합 | §1 시간 기준표 |
| L-1 파라미터 과다 | §13 |
| L-2 병렬 배열 | Trade UDT + array.remove |
| L-4 라벨/통계 불일치 | §8 표기 |
| L-5 ATR 역할 결합 | 역할별 상수 분리 (필터/SL/서킷 각각 명시) |

## 15. 검증 게이트 (비평 #4 반영 — 단방향 부등식 폐기)

1. 전략 `process_orders_on_close=true` vs 지표 집계 → **모호성 처리 차이만** 분리 (동일 체결 모델)
2. 전략 `false`(다음 봉 시가 체결) vs `true` → **순수 지연·슬리피지 비용** 측정
3. Bar Magnifier ON으로 동시 터치 실제 해소 → 지표 보수 가정의 상한 검증
4. 트리거 A/B/C (breakout/pattern/macd ± 패턴 가점): 5심볼 × 15m/1h, **1차 지표 = MAR·R/일**, 2차 = E[R]·승률·빈도·노출률
