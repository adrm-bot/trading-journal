# design-sync 노트 — 트레이딩 저널

- **진행(2026-07-06)**: 핸드오프 수령(바탕화면 `Trading journal dashboard redesign.zip` — README + dc.html 프로토타입).
  **1/3 대시보드 구현·배포 완료**(커밋 fd5f62c): 경고 스트립·오늘 할 일(라임 CTA 1개 규칙)·레짐 점진 공개·
  포지션 마진 컬럼·통계 6장·내비 뱃지·톤 레이어(앰비언트/인셋 하이라이트/유리질 셸). 문구는 워딩 규칙으로 작성.
  **남은 것**: 2/3 저널 화면(필터 바 개편·카드 상태 위계·인라인 복기 에디터 — 현재는 모달 재사용 중),
  3/3 인사이트 화면(확신도 캘리브레이션 다이버징 바·R 분포 확신도 스택·습관 비용 진단 확장).
  프로토타입 원본은 바탕화면 zip, README 불변 3종(UX 우선순위·토큰·데이터 계약) 준수할 것.
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
