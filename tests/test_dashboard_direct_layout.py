from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "app.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")


def test_dashboard_uses_direct_manipulation_without_edit_mode():
    assert 'id="layoutBtn"' not in HTML
    assert "function renderDashboardLegacy" not in HTML
    assert "function renderDashboard(el)" in HTML
    assert HTML.count("function renderDashboard(el)") == 1
    assert 'class="panel-grip"' in HTML
    assert 'class="panel-resize"' in HTML
    assert "dashLayout:v3:" in HTML
    assert "window.addEventListener('pointermove'" in HTML
    assert "sameRow=e.clientY>=r.top&&e.clientY<=r.bottom" in HTML
    assert "hit.closest('.dash-drop')===placeholder" in HTML


def test_dashboard_layout_has_grid_bounds_and_mobile_fallback():
    assert ".dash-grid{display:grid" in CSS
    assert "grid-template-columns:repeat(12" in CSS
    assert "grid-row:span var(--panel-rows" in CSS
    assert ".panel-resize" in CSS
    assert "cursor:nwse-resize" in CSS
    assert "@media(max-width:1080px)" in CSS
    assert ".panel-content{height:auto;overflow:visible" in CSS
    assert ".dash3" not in CSS


def test_editorial_scale_is_readable_on_wide_displays():
    assert "--fs-base:15px" in CSS
    assert "--fs-lg:20px" in CSS
    assert "--app-max:none" in CSS
    assert "line-height:1.6" in CSS


def test_dashboard_uses_terminal_chrome_without_floating_grip():
    assert "--onyx:#0b0e11" in CSS
    assert "--acid-lime:#fcd535" in CSS
    assert ".panel-chrome-title" in CSS
    assert 'class="jdock-expand"' in HTML
    assert "jdock-grip" not in HTML
    assert "dash-help" not in HTML
