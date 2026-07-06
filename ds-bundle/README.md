# 트레이딩 저널 (매매일지) — 스타일 가이드

**성격(정직 고지)**: 이 프로젝트는 정식 design-sync 산출물(리액트 컴포넌트 번들)이 아닙니다.
원본 앱이 FastAPI + Jinja + 바닐라 JS/CSS라 컴파일된 컴포넌트가 존재하지 않으며, 여기 있는 것은
**실제 배포 CSS(`_ds_journal.css` = app/static/app.css 사본)와 그 클래스 어휘를 그대로 쓰는
HTML 프리뷰 카드**입니다. 디자인 에이전트는 이 문서와 styles.css의 어휘로 룩을 재현할 수 있지만,
`window.*`로 import할 컴포넌트는 없습니다 — 마크업은 아래 클래스 조합으로 직접 작성해야 합니다.

## 셋업

프로바이더/래퍼 불필요. `styles.css`(→ `_ds_journal.css`)만 로드하면 `body`에 다크 테마
(배경 `--onyx`, 본문 `--mist`, Inter)가 적용됩니다. 폰트는 시스템 폴백(Inter/Berkeley Mono가
없으면 system-ui/monospace) — 원본 앱도 로컬 폰트에 의존하지 않습니다.

## 스타일 어휘 (전부 `_ds_journal.css`에 실존 — 새 이름을 만들지 말 것)

**CSS 변수(토큰)**
- 표면: `--bg` `--panel` `--card` `--card-2` `--plate` / 헤어라인 `--line` `--line-2`
- 텍스트: `--heading`(=`--snow`) `--text`(=`--mist`) `--muted`(=`--fog`) `--dim`
- 세만틱: 수익 `--pos`/`--pos-soft` · 손실 `--neg`/`--neg-soft` · 정보 `--info`/`--info-soft`
  · 주의 `--warn`(amber)/`--warn-soft` · CTA `--accent`(=`--acid-lime`)/`--accent-soft` · 포커스 `--focus`
- 라운드: `--r-card:12px` `--r-btn:6px` `--r-input:6px` `--r-badge:2px` `--r-pill`
- 간격: `--s1`(4)~`--s10`(40) / 글자: `--fs-micro`(10)~`--fs-xl`(23)
- 폰트: `--f-body` `--f-display` `--f-mono`
- 그림자·승격: `--inset` `--card-elev` `--glow` — 승격은 채움이 아니라 보더+그림자로

**클래스 패밀리**
- 텍스트 유틸: `.mono` `.num`(tabular-nums) `.pos` `.neg` `.muted` `.eyebrow` `.section-title`
- 버튼: `.btn` + `.primary`(라임, 화면당 1개) `.ghost` `.sm` `.sm.on`(토글 활성)
- 뱃지: `.badge` + `.long` `.short` `.done` `.pending` `.cross` `.liq` `.sv` `.pp`
- 카드: `.stat`(`.label` `.val`(+`.pos`/`.neg`) `.unit` `.msub`) · `.mchip`(`.mk` `.mv` `.ms`)
- 테이블: `.table-wrap > table` — `th`는 mono 대문자, 숫자 셀은 `.num`, 마지막 행 보더 없음
- 경고: `.cross-warn`(빨간 인라인 배너) · `.empty-inline`(빈 상태/한 줄 공지)
- 셸: `.shell`(230px 사이드 그리드) `.side` `.nav` `.topbar` `.content`

## 디자인 규칙 (원본 앱의 실제 규약)

1. 다크 온리. 배경 4단 스택(onyx→charcoal→obsidian→graphite) 밖의 회색을 만들지 않는다.
2. 라임(`--accent`)은 화면당 하나의 주요 CTA에만. 수익/손실 초록·빨강은 기능적 용도 전용.
3. 주의(amber)와 위험/위반(red)을 섞지 않는다.
4. 모든 숫자는 `.num`(mono + tabular-nums) — 갱신 시 흔들림 방지.
5. 사용자 대면 문구는 완결 문장으로 쓴다(압축·생략투 금지). 통계 없는 곳에 확률 표현 금지.

## 진실의 위치

스타일 정본: `_ds_journal.css` (원본 `app/static/app.css`). 카드 마크업: `components/<그룹>/<이름>/`.

## 조립 예시 (검증된 프리뷰에서 발췌)

```html
<div class="stat" style="background:var(--card);border:1px solid var(--line);border-radius:var(--r-card);padding:16px;box-shadow:var(--inset)">
  <div class="label">합산 손익</div>
  <div class="val pos num">+9,180.24<span class="unit">USDT</span></div>
  <div class="msub">최근 90일 · 41건</div>
</div>
```
