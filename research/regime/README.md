# 크립토 선물 레짐 분류기 — 5상태 + OI 리드-래그 검증

목적은 분류 정확도가 아니라 **전환 조기 감지**다. 산출물 두 개:

1. **현재 상태 분류기** — 매 15m 캔들(번인 후)에 `{regime, confidence∈[0,1], timestamp}` 방출.
   5상태 `TREND_UP / TREND_DOWN / SQUEEZE / RANGE / CHOP`, 플레이북(`playbook.py`)으로 전략
   on/off 연결. 예측·목표가·시그널 없음.
2. **OI 융합의 조기성 검증** — OI 융합 라벨(F)이 베이스라인을 몇 캔들 선행하는가.
   레짐엔 그라운드 트루스가 없으므로 정확도는 지표가 아님. **핵심 숫자 = OI 한계기여**
   (= lead(F vs B1) − lead(B2 vs B1); B2 = 연료 축 뺀 동일 분류기).

설계 전문과 적대 검증(에이전트 6, 21개 급소) 반영 내역: `NOTES.md` + `~/.claude/plans/task-crypto-futures-warm-peacock.md`.

## 핵심 결과 (BTCUSDT 15m, 2022-01→2026-07, 154k 캔들)

| 지표 | 값 |
|---|---|
| OI 한계기여 (1차 엔드포인트) | enter-TREND 풀링 **+1캔들** [월클러스터 CI 1, 2] · UP +2 [1,2] · DOWN +1 [0,2] |
| **★ 순환 시간이동 널 (200회)** | 널 중앙값 **+1.0, IQR [1,1], p=1.0** — 관측 +1과 **구별 불가** |
| OI 1스냅샷(5분) 지연 강건성 | +1 [1,2] (동일 — 널 해석과 정합) |
| 원시 F vs B1 (참고용) | 중앙값 −4(UP)/−6(DOWN): 원시 ADX보다 **늦게** 진입. 4개 era 전부 음(일관) |
| churn (3-슈퍼상태, flips/100봉) | B1 4.83 · **F 3.25** · B2 2.19 — F는 늦는 대신 훨씬 안정 |
| F 오경보율 | ~0.07 에피소드/100봉 (B1 미확인 추세 진입이 거의 없음) |
| hysteresis | `confirm=1`: churn −26%(3.25→2.41)·플랩 0.23→0.06에 **리드 희생 0** — 공짜 안정화 |
| 무튜닝 복제 | ETH/SOL/XRP 셋 다 한계기여 +1 [1,2]·churn 서열·점유율 구조 동일 재현 |

**정직한 결론 (널 보정):** 관측된 +1캔들 조기화는 시간이동(정렬 파괴) OI로도 그대로 재현된다
(p=1.0). 사분면의 방향 정보는 ΔOI가 아니라 **Δprice 레그**에서 나오므로, 이 +1은 "제3축이
실제 가격 방향으로 투표한다"는 구조 효과이지 **OI 정렬 정보가 아니다**. 즉 — 이 설정에서
**OI의 타이밍 알파는 검출되지 않았다.** 이 널 검정이 없었다면 "+1캔들 OI 엣지"(CI가 0을
제외!)를 오보로 출하했을 것이다 — 월클러스터 CI만으로는 부족했다는 것이 이 리서치의 핵심
방법론적 교훈. collapse 각도(2차)는 precision 0.982 vs 널 0.974(p=0.005)로 유의하지만
효과량이 미미하고 recall은 ns(p=0.22) — 약한 신호.

분류기 자체는 결과와 무관하게 출하: 5상태 전부 활성(점유율 9.6~48%), 체류 중앙값 7~13봉,
B1 대비 churn −33%·오경보 근절 — "늦지만 깨끗한" 레짐 게이트로서의 가치는 리드-래그와 별개.

## 파일

| 파일 | 역할 |
|---|---|
| `fetch_data.py` | Binance Vision 다운로드+캐시+체크섬. `python fetch_data.py BTCUSDT ...` |
| `build_dataset.py` | 인제스트·정렬 규약의 **유일한** 강제 지점 (정본 종가=open+15m, UTC ns, merge_asof backward, 갭 인벤토리) → parquet |
| `features.py` | 인과 피처 전부 (과거-전용 분위수, 상대 ΔOI(계약 수), 사분면) |
| `classifier.py` | 축 점수 → 5상태+confidence, 니어타이 현직 우선, 온라인 FSM hysteresis, degrade |
| `baseline.py` | B1 = 고전 ADX 3상태 (ADX≥25 + DI 부호) |
| `playbook.py` | `REGIME_PLAYBOOK` (분류기와 상호 import 금지 — 테스트로 강제) |
| `leadlag.py` | **에피소드(구간) 겹침 매칭** 하네스 + 월클러스터 부트스트랩 (포인트 매칭은 churn으로 게이밍 가능해 폐기 — NOTES 참조) |
| `run_all.py` | 스테이지 러너: headline / null / collapse / sweep |
| `current.py` | 라이브 스냅샷 CLI (REST, 무키): `python current.py BTCUSDT` |
| `viz.py` | 리본 / 리드-래그 스코어카드 / 전이 다이어그램 (Plotly HTML) |
| `tests/` | 28+ 테스트. **`test_no_lookahead.py`가 전체를 보호** — 원시 3테이블 절단/오염 + 카나리(심은 누수가 잡히는지 확인) + FSM 셀 불변성 |
| `results/` | 헤드라인·널·collapse·스윕 JSON + 시각화 HTML |

## 재현

```bash
pip install pandas numpy pyarrow plotly requests pytest
python fetch_data.py BTCUSDT ETHUSDT SOLUSDT XRPUSDT   # ~115MB, 재개 가능
python build_dataset.py BTCUSDT ETHUSDT SOLUSDT XRPUSDT
python -m pytest tests/ -q                              # 전부 녹색이어야 진행
python run_all.py BTCUSDT --stage all                   # headline+null+collapse+sweep (~15분)
python viz.py BTCUSDT
python current.py BTCUSDT                               # 지금 어떤 레짐인가
```

## 정직성 규약 (요약)

- 룩어헤드 테스트는 **원시 클라인·metrics·펀딩 3테이블 전부**를 벽시계 t*에서 절단/오염하고
  파이프라인 전체를 재실행, t* 이전 출력의 정확 일치를 요구. 카나리(OI −1캔들 시프트)가
  **실제로 빨간불**이 되는 것까지 테스트함.
- 매칭은 에피소드 단위·H 파라미터 없음. 오경보율이 1급 지표. 매처 자가검증 3종
  (노이지 비선행 ~0 / 노이지 선행 k 복원 / 갭)을 실데이터 전에 통과.
- 모든 분위수는 과거 전용. 번인 35일 제외. 갭 인접 에피소드 통계 제외.
- 파라미터는 실데이터 접촉 전 동결. 민감도 그리드(k×데드존, d×겹침)로 knife-edge 여부 공개.

## 한계

- OI 아카이브는 2021-12 이후만 존재 → 2022 이전 레짐 검증 불가. Binance 단일 거래소 OI.
- confidence는 확률이 아니라 축 합의 점수(상한 ~0.65) — collapse 탐지는 자기 분위수 기반이라 무영향.
- 30일 롤링 분위수 창보다 오래 지속되는 레짐은 서서히 재정규화됨(적응형 임계값의 본질).
- 리드-래그는 B1과 F가 공동 검출한 에피소드(매칭률 ~0.44)에 한정 — B1 단독 에피소드 대부분은
  F가 아예 추세로 보지 않은 단명 구간(오경보일 수도, 미탐일 수도 — 그라운드 트루스 없음).
