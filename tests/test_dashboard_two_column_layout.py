from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "app.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


def test_dashboard_keeps_two_column_workspace_and_journal_dock():
    assert 'class="dash3"' in HTML
    assert 'class="dash-mkt dash-zone" id="mktCol"' in HTML
    assert 'class="dash-me dash-zone" id="briefCol"' in HTML
    assert 'class="jdock" id="jdock"' in HTML
    assert (
        ".dash3{--brief-w:clamp(400px,27vw,520px);display:grid;"
        "grid-template-columns:minmax(0,1fr) 5px minmax(360px,var(--brief-w))"
    ) in CSS
    assert ".dash-resizer{position:relative;align-self:stretch" in CSS
    assert ".dash3.layout-edit .dash-resizer{cursor:col-resize}" in CSS
    assert "@media(min-width:961px)" in CSS
    assert "@media(max-width:1280px)" in CSS


def test_dashboard_uses_exchange_panel_chrome_inside_current_columns():
    rejected = (
        'id="layoutBtn"',
        "toggleDashboardEdit",
        "dashLayout:v1:",
        "bindDashResizer",
        "data-zone",
        "dash-grid",
    )
    for token in rejected:
        assert token not in HTML
    assert ".dash-grid" not in CSS
    assert "function dashLocked()" in HTML
    assert "return v==null?true:v==='1'" in HTML
    assert "locked?'레이아웃 편집':'편집 완료'" in HTML
    assert "function bindDashZone(zone)" in HTML
    assert 'class="panel-chrome"' in HTML
    assert 'class="panel-grip panel-drag"' in HTML
    assert 'class="panel-content"' in HTML
    assert 'class="panel-resize"' in HTML
    assert 'class="panel-dim"' in HTML
    assert "--r-btn:4px; --r-input:4px; --r-badge:2px; --r-card:3px" in CSS
    assert "border:1px solid transparent;border-radius:2px;background:var(--card)" in CSS
    assert ".panel-chrome{position:relative" in CSS
    assert "background:var(--card);border-bottom:1px solid var(--line)" in CSS
    assert ".dash-panel:hover>.panel-chrome,.dash-panel:focus-within>.panel-chrome" in CSS
    assert ".layout-edit .panel-resize{display:block" in CSS
    assert ".panel-ghost{" in CSS
    assert "ghost.style.transform=`translate3d(" in HTML
    assert "제목 바 이동 · 모서리 크기 조절" in HTML
    assert "DASH_PANEL_GEOMETRY" in HTML
    assert 'data-default-height="${g.height}"' in HTML
    assert 'data-min-height="${g.minHeight}"' in HTML
    assert "Number(s.height)||defaultH" in HTML
    assert "min-height:var(--panel-min-h,96px)" in CSS


def test_market_ordering_and_current_data_panels_remain_available():
    assert "function applyDashLayout()" in HTML
    assert "function saveDashZone(zone)" in HTML
    assert "window.addEventListener('pointermove',move,true)" in HTML
    assert "window.addEventListener('pointerup',finish,true)" in HTML
    assert "window.addEventListener('keydown',key,true)" in HTML
    assert "function updatePanelDim(panel,zone)" in HTML
    assert "ensureEconomy()" in HTML
    assert "function oiBlock(" in HTML
    assert "function oiQuantityBlock(" in HTML
    assert "function liqCard(" in HTML
    assert "marketPanel('econ'" in HTML
    assert "marketPanel('rs'," in HTML
    assert "marketPanel('flow'," in HTML
    assert "marketPanel('rsflow'," not in HTML
    assert "DASH_LAYOUT_REV=7" in HTML
    assert "k==='rsflow'?['rs','flow']" in HTML
    assert "canonicalMarket=['econ','strip','regime','liq','rs','flow','liqmap']" in HTML
    assert "ensureEconomy()" in HTML
    assert "oi_context_quad" in HTML
    assert "가격·OI 사분면" in HTML
    for label in ("신규 롱", "신규 숏", "숏 커버", "롱 청산"):
        assert label in HTML
    assert "ratio_asof" in HTML
    assert "ETH.D ${d.eth}%" in HTML
    assert "RS_HORIZONS" in HTML
    assert "btc_beta_30d" in HTML
    assert "rsSetHorizon" in HTML
    assert "기간별 상대강도 = 알트 수익률" in HTML
    assert "long_usd" in HTML and "short_usd" in HTML
    assert "e.confirmed?'확정':'예정'" in HTML


def test_journal_view_controls_change_only_dock_cards():
    for mode, label in (("summary", "요약"), ("balanced", "균형"), ("detail", "상세")):
        assert f"['{mode}','{label}']" in HTML
        assert f'#jdock[data-view="{mode}"]' in CSS
    assert "function applyJournalView(v,save)" in HTML
    assert "data-journal-view" in HTML
    assert "localStorage.setItem(dashKey('journal-view'),v)" in HTML
    assert "data-density-mode" not in HTML
    assert "applyDashDensity" not in HTML
    assert '#view-dashboard[data-density=' not in CSS
    assert '#jdock[data-view="summary"] .jcard:not(.pend) .jbody' in CSS
    assert '#jdock[data-view="summary"] .jstrategy' in CSS
    assert '#jdock[data-view="summary"] .jlevels-block' in CSS
    assert '#jdock[data-view="balanced"] .jreview' in CSS
    assert '#jdock[data-view="balanced"] .jmemo-block' in CSS
    assert '#jdock[data-view="balanced"] .jplan{display:-webkit-box' in CSS
    assert '#jdock[data-view="detail"] .jcard{min-height:184px' in CSS
    assert '#jdock[data-view="detail"] .jbody{margin-top:2px}' in CSS
    assert 'class="jtags jkeytags"' in HTML
    assert 'class="jsection jlevels-block' in HTML
    assert "['SL',r.sl,'lvsl'],['TP1',r.tp,'lvtp'],['TP2',r.tp2,'lvtp'],['TP3',r.tp3,'lvtp']" in HTML
    assert "journalTextBlock('jplan-block','진입 근거',r.plan,'jplan')" in HTML
    assert "journalTextBlock('jmemo-block','메모',r.memo,'jmemo')" in HTML
    assert '${planLevels(r)}' in HTML


def test_mobile_journal_views_keep_separate_heights_and_saved_state():
    assert "function mobileJournalMinHeight(v)" in HTML
    assert "v==='summary'?120:v==='detail'?260:168" in HTML
    assert "function mobileJournalDefaultHeight(v)" in HTML
    assert "function journalDockHeight(v)" in HTML
    assert "all.mobileDockHeights" in HTML
    assert "if(innerWidth<=560)setDockHeight(journalDockHeight(v),false)" in HTML


def test_dashboard_panel_spacing_is_fixed_across_journal_views():
    assert "--panel-pad-y:12px" in CSS
    assert "--panel-pad-x:12px" in CSS
    assert "--nested-pad-y:10px" in CSS
    assert "--nested-pad-x:10px" in CSS
    assert "--panel-shell-pad:10px" in CSS
    assert '.dash-panel[data-panel="regime"] .panel-content{padding:10px}' in CSS
    assert "--dash-gap:12px" in CSS
    assert "el.dataset.density" not in HTML


def test_dashboard_uses_single_column_brief_panels_with_compact_internal_grids():
    assert ".dash-me .cards,.dash-me .mgrid{grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:auto}" in CSS
    assert ".dash-me .mgrid{grid-template-columns:repeat(2,minmax(0,1fr))" in CSS
    assert "#view-dashboard .dash-me .stat .val{margin-top:4px;font-size:19px" in CSS
    assert ".dash-panel:hover,.dash-panel:focus-within{border-color:var(--line-2)}" in CSS
    assert ".panel-grip svg{width:14px;height:14px;display:block;opacity:0" in CSS
    assert ".dash3:not(.layout-edit) .panel-grip svg{opacity:0}" in CSS


def test_market_defaults_are_compact_and_relative_strength_is_split_from_flow():
    assert "const DASH_LAYOUT_REV=7" in HTML
    assert "canonicalMarket=['econ','strip','regime','liq','rs','flow','liqmap']" in HTML
    assert "regime:{height:320,minHeight:280}" in HTML
    assert "liq:{height:320,minHeight:220}" in HTML
    assert "rs:{height:260,minHeight:180}" in HTML
    assert "flow:{height:260,minHeight:180}" in HTML
    assert "metrics:{height:220,minHeight:160}" in HTML
    assert "discipline:{height:200,minHeight:160}" in HTML
    assert "behavior:{height:180,minHeight:120}" in HTML
    assert "marketPanel('regime'" in HTML and "{span:4,min:3}" in HTML
    assert "marketPanel('liq'" in HTML and "{span:8,min:4}" in HTML
    assert "marketPanel('flow'" in HTML and "{span:4,min:3}" in HTML
    assert "marketPanel('rs'" in HTML and "{span:8,min:4}" in HTML
    assert ".dash-mkt{position:relative;display:grid;grid-template-columns:repeat(12,minmax(0,1fr));align-content:start;align-items:start" in CSS


def test_market_grid_keeps_stable_order_and_equalizes_visual_rows():
    assert "grid-auto-flow:row;grid-auto-rows:8px" in CSS
    assert "grid-auto-flow:row dense" not in CSS
    assert ".dash-mkt>.dash-panel{grid-row-end:span var(--panel-row-span,12);align-self:stretch}" in CSS
    assert ".panel-placeholder{grid-column:span var(--panel-span,12);grid-row-end:span var(--panel-row-span,12)" in CSS
    assert "function syncPanelGridRow(panel)" in HTML
    assert "function syncDashboardRows()" in HTML
    assert "const mx=Math.max(...row.map(panelOwnGridSpan))" in HTML
    assert "Math.ceil((h+gap)/(row+gap))" in HTML
    assert "syncPanelGridRow(panel);syncDashboardRows();updatePanelDim(panel,zone)" in HTML
    assert "ph.style.setProperty('--panel-row-span'" in HTML
    assert ".dash-mkt>.dash-panel{grid-column:1!important;grid-row:auto!important}" in CSS


def test_dashboard_resize_and_drop_use_deliberate_snap_points():
    assert "const DASH_SPAN_STOPS=[3,4,6,8,9,12]" in HTML
    assert "const DASH_STACK_BP=1280" in HTML
    assert "function snapPanelSpan(" in HTML
    assert "function snapPanelHeight(" in HTML
    assert "Math.round(Number(value||min)/20)*20" in HTML
    assert "function dashDropRef(" in HTML
    assert "function panelEffectiveMinHeight(" in HTML
    assert "콘텐츠 최소" in HTML
    assert "zone.classList.add('grid-active')" in HTML
    assert "zone.classList.remove('grid-active')" in HTML
    assert "폭 ${span}/12 · 높이 ${h}px" in HTML
    assert ".dash-mkt.grid-active{" in CSS
    assert "width:var(--ghost-w,180px)" in CSS


def test_oi_quadrant_uses_korean_type_and_lives_in_positioning_not_regime():
    assert "const S=244,C=S/2,PAD=28" in HTML
    assert 'class="rgquad-label"' in HTML
    assert 'font-family="monospace"' not in HTML
    assert "function oiContextQuadrant()" in HTML
    assert "가격·OI 사분면" in HTML
    assert "포지셔닝 맥락 · 레짐 미반영" in HTML
    assert 'class="rgq-figure"' in HTML
    assert 'class="rgquad-readout"' in HTML
    assert "가격 변화 × OI 변화 · 2시간" in HTML
    assert 'class="oi-context-grid">${quad}<div class="oi-flow-stack">${crowd}${taker}' in HTML
    assert '<div class="rgrow">${quad}' not in HTML
    assert "rgquad-cap" not in HTML
    assert ".oi-context-quad{display:grid" in CSS
    assert ".rgquad-label{font-family:var(--f-body)" in CSS


def test_dashboard_uses_full_viewport_width_and_equal_height_journal_rows():
    assert ".content{width:100%;margin:0;padding:0 var(--page-pad) var(--s8)}" in CSS
    assert ".pagehead{display:flex;align-items:flex-end;justify-content:space-between;gap:var(--s4);width:100%" in CSS
    assert ".jdock-inner{width:100%;margin:0;padding:0 var(--page-pad)}" in CSS
    assert ".jdock-body .jfeed{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:var(--s3);align-items:stretch}" in CSS
    assert ".jdock-body .jcard{margin:0;min-height:172px;height:100%;overflow:visible;position:relative}" in CSS


def test_market_panels_share_one_vertical_scroll_per_column():
    assert ".dash-mkt>.dash-panel,.dash-me>.dash-panel{height:auto!important;min-height:max(var(--panel-user-h" in CSS
    assert "var(--panel-natural-h,0px));max-height:none;grid-template-rows:30px auto" in CSS
    assert ".dash-mkt>.dash-panel>.panel-content,.dash-me>.dash-panel>.panel-content{overflow:visible" in CSS
    assert "grid-template-columns:minmax(0,1fr) 5px minmax(360px,var(--brief-w));gap:0" in CSS
    assert "padding-right:var(--dash-gap,var(--s4));container:marketcol/inline-size" in CSS
    assert "padding-left:var(--dash-gap,var(--s4));container:briefcol/inline-size" in CSS
    assert '.dash-resizer::before{content:"";position:absolute;left:0' in CSS
    assert "--panel-user-h" in HTML
    assert "function bindDashboardNaturalHeights()" in HTML
    assert "#view-dashboard .dash-zone > .dash-panel,#view-dashboard .dash-urgent > .dash-panel" in HTML
    assert "new ResizeObserver(sync)" in HTML
    assert "zone.id==='mktCol'?parseInt(p.style.getPropertyValue('--panel-user-h')" in HTML


def test_average_r_labels_describe_realized_r_without_colliding_with_payoff_ratio():
    assert "평균 실현 R (30일)" in HTML
    assert "mcard('평균 실현 R'" in HTML
    assert "mcard('손익비'" in HTML
    assert "평균 손익비" not in HTML


def test_dashboard_responds_to_panel_width_without_clipping_copy():
    assert "container:marketcol/inline-size" in CSS
    assert "container:briefcol/inline-size" in CSS
    assert "@container marketcol (max-width:760px)" in CSS
    assert "@container briefcol (max-width:360px)" in CSS
    assert "container:dashboard-panel/inline-size" in CSS
    assert "@container dashboard-panel (max-width:1100px)" in CSS
    assert '.dash-panel[data-panel="strip"] .market{grid-template-columns:repeat(2,minmax(0,1fr))}' in CSS
    assert "@container dashboard-panel (max-width:520px)" in CSS
    assert ".crowd-deltas{grid-template-columns:repeat(2,minmax(0,1fr))}" in CSS
    assert ".taker{grid-template-columns:1fr auto" in CSS
    assert ".wstrip-item .wtxt{overflow:visible;text-overflow:clip;white-space:normal}" in CSS
    assert ".secn{min-width:0;overflow:visible;text-overflow:clip;white-space:normal" in CSS


def test_market_context_natural_height_has_no_flex_feedback_loop():
    assert '#marketStrip{height:auto;min-height:0;display:flex;flex-direction:column}' in CSS
    assert '.dash-panel[data-panel="strip"] .market{flex:none;' in CSS


def test_relative_strength_ranks_btc_as_an_explicit_baseline_and_starts_collapsed():
    assert "const btcRef={b:{sym:'BTC',is_btc:true}" in HTML
    assert "const rankedAll=[...ranked,btcRef]" in HTML
    assert "상대강도 기준선" in HTML
    assert '<details class="rsmore"><summary>' in HTML
    assert '<details class="rsmore" open>' not in HTML


def test_korean_information_hierarchy_uses_body_type_not_monospace():
    assert "--f-mono:'JetBrains Mono','Pretendard','Pretendard Variable',\"Malgun Gothic\"" in CSS
    assert ".eyebrow{font-family:var(--f-mono)" in CSS
    assert ".section-title{font-family:var(--f-body);font-size:var(--fs-sm);font-weight:620" in CSS
    assert ".panel-chrome-title{position:relative" in CSS
    assert "font-size:var(--fs-sm);font-weight:620" in CSS
    assert ".pos-title-note{font-family:var(--f-body)" in CSS


def test_methodology_copy_uses_accessible_help_controls():
    assert "function helpTip(" in HTML
    assert 'class="help-tip"' in HTML
    assert "document.addEventListener('focusin'" in HTML
    assert "레짐 산정 기준" in HTML
    assert "OI 규모 통계 기준" in HTML
    assert "70·90백분위는 상위 30%·10%를 나눈 설명용 구간" in HTML
    assert "경험적 분위 구간 · 예측 임계값 아님" in HTML
    assert "Binance 원자료 ↗" in HTML


def test_liquidation_history_and_predictive_map_are_labeled_as_different_products():
    assert "실제 청산 체결" in HTML
    assert "이미 발생한 강제청산 체결 · 예측형 청산맵 아님" in HTML
    assert "가격대별 실제 청산 체결" in HTML
    assert "예측형 청산맵" in HTML
    assert "무료 공개 청산 체결 데이터와는 별도 기능" in HTML


def test_review_queue_and_journal_cards_share_symbol_and_side_treatment():
    assert HTML.count('class="badge trade-side ${') == 2
    assert 'class="trade-symbol">${esc(r.symbol)}</b>${dir}' in HTML
    assert 'class="sym trade-symbol">${esc(t.symbol)}</span>' in HTML
    assert ".badge.trade-side{min-width:28px;height:22px" in CSS


def test_dashboard_keeps_empty_state_panels_and_persistent_ledger_status():
    assert "규율 지표 산출 대기" in HTML
    assert "복기 대기 없음" in HTML
    assert "Binance 원장 일치" in HTML
    assert "Binance 원장 불일치" in HTML
    assert "DATA&&DATA.sync_audits" in HTML
    assert '"sync_audits": db.get_sync_audits(uid)' in (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert ".panel-empty-state{" in CSS
    assert ".sync-audit{" in CSS


def test_open_positions_show_quantity_and_all_planned_targets():
    assert 'class="pos-qty">수량 ${fmtP(p.qty)}' in HTML
    assert "TP1 ${fmtP(_tp)}" in HTML
    assert "TP2 ${fmtP(_tp2)}" in HTML
    assert "TP3 ${fmtP(_tp3)}" in HTML
    assert "function exchangeExitLevels(p)" in HTML
    assert "동일 가격 주문 ${o.order_count}건" in HTML
    assert "exchange_exit_orders" in HTML
    assert "거래소 주문" in HTML
    assert "내 계획" in HTML
    assert 'class="pos-list" role="table"' in HTML
    assert 'class="pos-row${p.margin_mode===' in HTML
    assert "@container dashboard-panel (max-width:460px)" in CSS
    assert 'grid-template-areas:"main dir pnl" "price price price" "orders orders orders" "plan plan plan"' in CSS


def test_layout_rev7_preserves_custom_geometry_and_prioritizes_execution_panels_when_stacked():
    assert "panels:cfg.panels&&typeof cfg.panels==='object'?{...cfg.panels}:{}" in HTML
    assert "same(next,oldCanonical)" in HTML
    assert "canonical.forEach(k=>{if(!next.includes(k))next.push(k);})" in HTML
    assert 'class="dash-urgent" id="briefUrgent" hidden' in HTML
    assert "['today','positions'].forEach" in HTML
    assert "function syncDashboardStack()" in HTML
    assert "bindDashboardWorkspace();syncDashboardStack();bindJdock()" in HTML
    assert ".dash-urgent:not([hidden]){display:grid" in CSS
    assert "stacked?'자동 배치':locked?'레이아웃 편집':'편집 완료'" in HTML
    assert ".dash3 .panel-grip,.dash3 .panel-resize,.dash3 .panel-dim{display:none!important}" in CSS
    assert "#view-dashboard .dash3 .panel-grip," in CSS


def test_drag_and_resize_cancel_without_persisting_changes():
    assert "ev.key==='Escape'" in HTML
    assert "settle(true)" in HTML
    assert "done(true)" in HTML
    assert "panel.style.setProperty('--panel-span',startSpan)" in HTML
    assert "panel.style.setProperty('--panel-user-h',startUserH+'px')" in HTML
    assert "if(!cancel)saveDashZone(zone)" in HTML


def test_behavior_correction_queue_is_visible_on_dashboard_and_insights():
    assert "function correctionQueue(ts,limit)" in HTML
    assert "최근 20건과 직전 20건 비교" in HTML
    assert "correctionQueue(DATA.trades,3)" in HTML
    assert "correctionQueue(ts,6)" in HTML
    assert ".correction-queue{" in CSS
    assert ".corr-row{" in CSS


def test_insight_sections_do_not_disappear_when_optional_annotations_are_empty():
    assert "현재 관측기간에는 확정된 낙폭 회복 구간이 없습니다" in HTML
    assert "진입근거 태그를 입력하면 근거별 성과가 집계됩니다" in HTML
    assert "진입 전 감정을 기록하면 감정별 성과가 집계됩니다" in HTML
    assert "복기에서 실수 태그를 기록하면 반복 손실 패턴이 표시됩니다" in HTML
