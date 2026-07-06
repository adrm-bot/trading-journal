# design-sync 노트 — 트레이딩 저널

- **대기 중(2026-07-06)**: Claude Design에 대시보드/저널/인사이트 리디자인 프롬프트 전달됨(레포 밖).
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
