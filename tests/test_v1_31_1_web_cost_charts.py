"""Tests for v1.31.1 — web Cost panel real visualization.

Replaces the text-only cost panel with a budget gauge + daily-rollup
SVG bar chart, both driven by the v1.28.2 budget_status() and
daily_totals() data. New endpoint /api/cost-detail returns structured
data; /api/cost-summary preserved as the back-compat text path.

DESIGN INVARIANTS PINNED HERE:
  * Gauge HIDDEN when budget unconfigured. Don't show an empty 0/0 bar.
  * Chart is inline SVG — no charting library bundled.
  * /api/cost-detail returns {summary, turn, budget, daily, days}
    and gates via _gate_get (read-only).
  * /api/cost-summary kept untouched for back-compat (older clients
    still work).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from janus import cost
from janus.gateways import web as web_mod


_STATIC = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================
# /api/cost-detail endpoint
# ============================================================


def test_cost_detail_endpoint_registered():
    src = inspect.getsource(web_mod)
    assert "/api/cost-detail" in src


def test_cost_detail_is_read_only():
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_cost_detail")
    assert region_start != -1
    region = src[region_start:region_start + 2500]
    assert "_gate_get" in region
    assert "_gate_post" not in region


def test_cost_detail_returns_structured_payload():
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_cost_detail")
    region = src[region_start:region_start + 2500]
    # Pulls in all four primitives
    assert "budget_status" in region
    assert "turn_stats" in region
    assert "daily_totals" in region
    assert "render_summary" in region


def test_cost_detail_clamps_days():
    """Defensive: caller may pass days=99999 or days=-1; clamp to
    a sane range so we don't try to scan ledger entries from year 1."""
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_cost_detail")
    region = src[region_start:region_start + 2500]
    assert "max(1" in region
    assert "min(int(days" in region or "min(int(days or" in region


def test_cost_summary_endpoint_preserved():
    """v1.31.1 must NOT delete /api/cost-summary — older clients
    (older app.js / external scripts) still hit it."""
    src = inspect.getsource(web_mod)
    assert "/api/cost-summary" in src


# ============================================================
# Behavioral via FastAPI TestClient
# ============================================================


def _build_test_client(monkeypatch, tmp_path):
    from janus import config as cfg
    monkeypatch.setattr(cfg, "HOME", tmp_path)
    monkeypatch.setattr(cfg, "WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        web_mod, "_check_auth", lambda req: ("test-sid", None),
    )
    monkeypatch.setattr(web_mod, "_check_csrf", lambda req, sid: True)
    from janus.gateways import web_auth
    monkeypatch.setattr(
        web_auth, "rate_limit_take", lambda sid, kind: (True, 0.0),
    )
    from fastapi.testclient import TestClient
    return TestClient(web_mod._build_app())


def test_get_cost_detail_unconfigured_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cost, "budget_status",
        lambda: {"budget": 0.0, "spent": 0.0, "remaining": 0.0,
                 "percent": 0.0, "configured": False},
    )
    monkeypatch.setattr(cost, "daily_totals", lambda *, since_days=14: [])
    monkeypatch.setattr(cost, "render_summary", lambda: "(no usage yet)")
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.get("/api/cost-detail")
    assert r.status_code == 200
    body = r.json()
    assert body["budget"]["configured"] is False
    assert body["daily"] == []
    assert body["days"] == 14
    assert "turn" in body
    assert "summary" in body


def test_get_cost_detail_with_data(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cost, "budget_status",
        lambda: {"budget": 5.0, "spent": 1.25, "remaining": 3.75,
                 "percent": 0.25, "configured": True},
    )
    fake_daily = [
        {"date": "2026-05-07", "calls": 12, "prompt_tokens": 1000,
         "completion_tokens": 200, "usd": 0.42},
        {"date": "2026-05-06", "calls": 5, "prompt_tokens": 500,
         "completion_tokens": 100, "usd": 0.18},
    ]
    monkeypatch.setattr(
        cost, "daily_totals", lambda *, since_days=14: fake_daily,
    )
    monkeypatch.setattr(cost, "render_summary", lambda: "summary text")
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.get("/api/cost-detail?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 7
    assert body["budget"]["configured"] is True
    assert body["budget"]["percent"] == 0.25
    assert len(body["daily"]) == 2
    assert body["daily"][0]["date"] == "2026-05-07"


def test_get_cost_detail_clamps_negative_days(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cost, "budget_status",
        lambda: {"configured": False, "budget": 0, "spent": 0,
                 "remaining": 0, "percent": 0},
    )
    captured: list[int] = []

    def fake_totals(*, since_days=14):
        captured.append(since_days)
        return []

    monkeypatch.setattr(cost, "daily_totals", fake_totals)
    monkeypatch.setattr(cost, "render_summary", lambda: "")
    client = _build_test_client(monkeypatch, tmp_path)
    client.get("/api/cost-detail?days=-5")
    # Clamped to >=1
    assert captured[0] >= 1


def test_get_cost_detail_clamps_huge_days(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cost, "budget_status",
        lambda: {"configured": False, "budget": 0, "spent": 0,
                 "remaining": 0, "percent": 0},
    )
    captured: list[int] = []
    monkeypatch.setattr(
        cost, "daily_totals",
        lambda *, since_days=14: (captured.append(since_days), [])[1],
    )
    monkeypatch.setattr(cost, "render_summary", lambda: "")
    client = _build_test_client(monkeypatch, tmp_path)
    client.get("/api/cost-detail?days=99999")
    assert captured[0] <= 90


# ============================================================
# Static asset additions (HTML + JS + CSS)
# ============================================================


def test_index_html_has_budget_block():
    html = _read(_STATIC / "index.html")
    assert 'id="cost-budget-block"' in html
    assert 'id="cost-gauge-fill"' in html
    assert 'id="cost-budget-state"' in html


def test_index_html_has_chart_block():
    html = _read(_STATIC / "index.html")
    assert 'id="cost-chart"' in html
    assert 'id="cost-chart-empty"' in html


def test_index_html_has_window_selector():
    """User can pick 7 / 14 / 30 / 90 day window."""
    html = _read(_STATIC / "index.html")
    assert 'id="cost-window"' in html
    for v in ("7", "14", "30", "90"):
        assert f'value="{v}"' in html


def test_index_html_budget_starts_hidden():
    """Hide the gauge when JANUS_BUDGET_USD unset (rendered shows it)."""
    html = _read(_STATIC / "index.html")
    start = html.find('id="cost-budget-block"')
    end = html.find(">", start)
    tag = html[start:end + 1]
    assert "display:none" in tag.replace(" ", "")


def test_app_js_render_cost_budget():
    js = _read(_STATIC / "app.js")
    assert "renderCostBudget" in js


def test_app_js_render_cost_chart():
    js = _read(_STATIC / "app.js")
    assert "renderCostChart" in js


def test_app_js_uses_cost_detail_endpoint():
    js = _read(_STATIC / "app.js")
    assert "/api/cost-detail" in js


def test_app_js_chart_is_inline_svg():
    """No external charting library — chart renders via createElementNS
    on the SVG namespace."""
    js = _read(_STATIC / "app.js")
    region_start = js.find("renderCostChart")
    region = js[region_start:region_start + 4000]
    assert "createElementNS" in region
    assert "svg" in region


def test_app_js_chart_handles_empty_daily():
    js = _read(_STATIC / "app.js")
    region_start = js.find("renderCostChart")
    region = js[region_start:region_start + 4000]
    # Empty array → show "no usage in window" message and return.
    assert "cost-chart-empty" in region


def test_app_js_budget_gauge_hides_when_unconfigured():
    js = _read(_STATIC / "app.js")
    region_start = js.find("renderCostBudget")
    region = js[region_start:region_start + 2500]
    assert "configured" in region
    assert "display = 'none'" in region


def test_app_js_budget_gauge_marks_over():
    """At >=100% the fill swaps to the 'over' (solid red) state."""
    js = _read(_STATIC / "app.js")
    region_start = js.find("renderCostBudget")
    region = js[region_start:region_start + 2500]
    assert "'over'" in region or '"over"' in region


def test_app_js_window_selector_triggers_reload():
    js = _read(_STATIC / "app.js")
    region_start = js.find("registerPanel('cost'")
    region = js[region_start:region_start + 4000]
    assert "windowSel.onchange" in region or "windowSel.onchange =" in region


def test_app_css_has_gauge_styles():
    css = _read(_STATIC / "app.css")
    assert ".cost-gauge-track" in css
    assert ".cost-gauge-fill" in css
    assert ".cost-gauge-fill.over" in css


def test_app_css_has_chart_styles():
    css = _read(_STATIC / "app.css")
    assert ".cost-chart" in css
    assert ".cost-bar" in css
