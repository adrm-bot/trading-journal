from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "app.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


def test_dashboard_restores_stable_two_column_shell_and_journal_dock():
    assert 'class="dash3"' in HTML
    assert 'class="dash-mkt" id="mktCol"' in HTML
    assert 'class="dash-me"' in HTML
    assert 'class="jdock" id="jdock"' in HTML
    assert (
        ".dash3{display:grid;"
        "grid-template-columns:minmax(300px,1fr) 1px 620px"
    ) in CSS
    assert ".dash-resizer{align-self:stretch;background:var(--line)" in CSS


def test_dashboard_removes_rejected_layout_experiments():
    rejected = (
        'id="layoutBtn"',
        "toggleDashboardEdit",
        "dashLayout:v1:",
        "bindDashResizer",
        "panel-size",
        "data-span",
        "data-zone",
        "dash-grid",
        "panel-resize",
    )
    for token in rejected:
        assert token not in HTML
    assert ".panel-size" not in CSS
    assert ".dash-grid" not in CSS


def test_market_ordering_and_current_data_panels_remain_available():
    assert 'draggable="true"' in HTML
    assert "function bindMktDnD()" in HTML
    assert "function applyMktOrder()" in HTML
    assert "ensureEconomy()" in HTML
    assert "function oiBlock(" in HTML
    assert "function liqCard(" in HTML


def test_dense_dock_never_hides_trade_detail():
    assert ".jdock-body.cols-4 .jbody" not in CSS
    assert ".jdock-body.cols-4 .jlegs" not in CSS
    assert ".jdock-body.cols-4 .jlevels" not in CSS
    assert ".jdock-body .jcard{margin:0;min-height:172px;height:auto;overflow:visible" in CSS
