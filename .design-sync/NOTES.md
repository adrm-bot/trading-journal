# design-sync 노트 — 트레이딩 저널

- **결론(2026-07-06): 전면 리디자인 기각, 선별 채택으로 확정** (커밋 0b6b891).
  fd5f62c의 전면 구현을 사용자가 롤백 지시("기존대로 롤백하고 반영할만한 아이디어만") —
  직접 원인: 레짐 상세 접힘 기본값이 OI 사분면을 숨김. 교훈: **이 사용자는 레이아웃·정보 노출을
  줄이는 변경(점진 공개·컬럼 재배치·톤 변경)을 원치 않음. 기존 화면 위에 얹는 추가형 개선만 채택할 것.**
  - 채택되어 배포된 것: 규율 경고 스트립(딥링크 포함) · 오늘 할 일 카드(미복기 N건, 0건이면 숨김) ·
    포지션 마진 컬럼+위반 행 · 평균 R(30일)·프로핏 팩터 통계 · 사이드바 미복기 뱃지
  - 기각된 것: 레이아웃 재배치(dash3+리사이저 유지), 레짐 점진 공개(사분면·리본·성과표 상시 표시 유지),
    톤 레이어(앰비언트/유리질/인셋 하이라이트), 라임 1개 규칙(새로고침 라임 유지), 테이블 다이어트류.
  - 핸드오프 2/3(저널)·3/3(인사이트) 화면 작업은 **보류** — 진행하려면 위 교훈대로 추가형으로만.
  프로토타입 원본: 바탕화면 `Trading journal dashboard redesign.zip`.
- (이전) Claude Design에 대시보드/저널/인사이트 리디자인 프롬프트 전달됨(레포 밖).
  시안이 돌아오면 한 사이클로 처리하기로 함 — ① 시안을 app.html/app.css 토큰 체계로 구현(규약 위반은 필터링)
  ② adrm-natural-writing 스킬로 사용자 대면 문구 전면 패스(확정 용어 맵·완결 문장 규칙은 메모리 ui-wording-preference 참조)
  ③ pytest + DEV E2E + Render 배포.

- 2026-07-06 첫 시도: 저장소에 package.json/Storybook/노드 빌드 없음(전수 Glob 확인) — 정식 sync 불가.
  사용자 선택 = "스타일 가이드만 수동 제작".
- `ds-bundle/` 구성: `styles.css` → `_ds_journal.css`(= `app/static/app.css` 사본) @import,
  프리뷰 카드 8장(Foundations: Colors·Typography / Components: Buttons·Badges·StatCard·MarketChip·Table / Patterns: CrossWarn),
  각 카드 첫 줄 `@dsCard` 마커. `_ds_bundle.js`·`_ds_sync.json`은 정직하게 생략(컴파일 컴포넌트 없음 — 오프계약 스타일 참조 프로젝트).
- 카드의 모든 클래스·변수는 app.css에 실존함을 grep으로 검증(.badge.cross 253행, .badge.pp 351행, .btn.sm 122행, .cross-warn 254행 등).
- 로컬 렌더 검증: `.claude/launch.json`의 `ds-preview`(http.server 8010, ds-bundle 서빙)로 8장 전부 200 + 계산 스타일 확인
  (body bg #08090a, badge.long = pos-soft/mint).
- **차단 지점**: 이 세션(비대화형)에서 DesignSync 인증 불가 — "/design-login requires an interactive terminal".
  다음 실행 절차: 대화형 터미널에서 `claude` → `/design-login` → `/design-sync` 재실행 →
  list_projects로 이름 충돌 확인 후 create_project("Trading Journal — 매매일지") → projectId를 이 config에 기록 →
  finalize_plan(localDir=./ds-bundle) → 업로드(센티널 → 파일 → 센티널 재무장).
- app.css가 바뀌면 `cp app/static/app.css ds-bundle/_ds_journal.css` 후 재업로드.
