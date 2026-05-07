"""Tests for v1.31.2 — persistent skill grants web panel.

The /grants slash command (v1.24.1) lets cli_rich + telegram users
revoke persistent grants stored in ~/.janus/approvals.json. v1.31.2
brings the same surface to the web UI: dedicated panel with per-row
revoke buttons + clear-all.

DESIGN INVARIANTS PINNED HERE:
  * Reads + writes hit ``~/.janus/approvals.json`` (P5 plain-text
    persistent state). Same source of truth all surfaces use.
  * Revoke + clear are mutating endpoints — gated via ``_gate_post``
    (auth + rate-limit + CSRF + audit).
  * 404 when revoking a (tool, risk) pair that doesn't exist —
    distinct from 500 so the client can tell.
  * Session grants NOT exposed via the web (they're per-HTTP-request
    and would be empty in a stateless fetch anyway).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from janus import permissions
from janus.gateways import web as web_mod


_STATIC = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================
# Endpoint registration + source pins
# ============================================================


def test_three_grants_endpoints_registered():
    src = inspect.getsource(web_mod)
    assert '"/api/grants"' in src or "'/api/grants'" in src
    assert "/api/grants/revoke" in src
    assert "/api/grants/clear" in src


def test_grants_get_is_read_only():
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_grants(")
    region = src[region_start:region_start + 2000]
    assert "_gate_get" in region


def test_grants_revoke_is_mutating():
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_grants_revoke")
    region = src[region_start:region_start + 2500]
    assert "_gate_post" in region
    assert "web_audit.mutate" in region


def test_grants_clear_is_mutating():
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_grants_clear")
    region = src[region_start:region_start + 1500]
    assert "_gate_post" in region
    assert "web_audit.mutate" in region


def test_grants_uses_shared_source_of_truth():
    """Same ~/.janus/approvals.json that cli_rich + telegram see —
    NOT a parallel web-only state file."""
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_grants(")
    region = src[region_start:region_start + 4000]
    assert "_load_persistent_grants" in region


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
    from janus.gateways import web_auth, web_audit
    monkeypatch.setattr(
        web_auth, "rate_limit_take", lambda sid, kind: (True, 0.0),
    )
    monkeypatch.setattr(web_audit, "mutate", lambda *a, **k: None)
    from fastapi.testclient import TestClient
    return TestClient(web_mod._build_app())


def _seed_grants(tmp_path: Path, grants: set) -> None:
    """Write ~/.janus/approvals.json with the given grants set."""
    payload = {
        "version": 1,
        "grants": [{"tool": t, "risk": r} for (t, r) in sorted(grants)],
    }
    (tmp_path / "approvals.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def test_get_grants_empty(monkeypatch, tmp_path):
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.get("/api/grants")
    assert r.status_code == 200
    body = r.json()
    assert body["grants"] == []
    assert body["total"] == 0


def test_get_grants_lists_persisted(monkeypatch, tmp_path):
    _seed_grants(tmp_path, {("fs_write", "write"), ("shell", "exec")})
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.get("/api/grants")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    tools = {g["tool"] for g in body["grants"]}
    assert tools == {"fs_write", "shell"}


def test_get_grants_sorted_alphabetically(monkeypatch, tmp_path):
    _seed_grants(tmp_path, {
        ("zzz_tool", "exec"),
        ("aaa_tool", "write"),
        ("mmm_tool", "read"),
    })
    client = _build_test_client(monkeypatch, tmp_path)
    body = client.get("/api/grants").json()
    tools = [g["tool"] for g in body["grants"]]
    assert tools == ["aaa_tool", "mmm_tool", "zzz_tool"]


def test_post_revoke_drops_grant(monkeypatch, tmp_path):
    _seed_grants(tmp_path, {("fs_write", "write"), ("shell", "exec")})
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/grants/revoke",
        json={"tool": "fs_write", "risk": "write"},
        headers={"X-CSRF-Token": "any"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["remaining"] == 1
    # Reload — only shell/exec should remain
    remaining = permissions._load_persistent_grants()
    assert remaining == {("shell", "exec")}


def test_post_revoke_404_when_missing(monkeypatch, tmp_path):
    _seed_grants(tmp_path, {("fs_write", "write")})
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/grants/revoke",
        json={"tool": "shell", "risk": "exec"},
        headers={"X-CSRF-Token": "any"},
    )
    assert r.status_code == 404
    # File untouched
    remaining = permissions._load_persistent_grants()
    assert remaining == {("fs_write", "write")}


def test_post_revoke_400_when_body_incomplete(monkeypatch, tmp_path):
    _seed_grants(tmp_path, {("fs_write", "write")})
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/grants/revoke",
        json={"tool": "fs_write"},  # missing risk
        headers={"X-CSRF-Token": "any"},
    )
    assert r.status_code == 400


def test_post_clear_wipes_all(monkeypatch, tmp_path):
    _seed_grants(tmp_path, {
        ("a", "exec"), ("b", "write"), ("c", "read"),
    })
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/grants/clear",
        json={},
        headers={"X-CSRF-Token": "any"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == 3
    remaining = permissions._load_persistent_grants()
    assert remaining == set()


def test_post_clear_when_already_empty(monkeypatch, tmp_path):
    """No-op when nothing to clear; still 200."""
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/grants/clear",
        json={},
        headers={"X-CSRF-Token": "any"},
    )
    assert r.status_code == 200
    assert r.json()["removed"] == 0


# ============================================================
# Static-asset additions
# ============================================================


def test_index_html_has_grants_nav_entry():
    html = _read(_STATIC / "index.html")
    assert 'data-panel="grants"' in html
    assert 'href="#grants"' in html


def test_index_html_grants_nav_after_cost():
    """v1.31.2 placement: between cost and mcp (settings/grants
    sandwich would split grouping)."""
    html = _read(_STATIC / "index.html")
    cost_idx = html.find('data-panel="cost"')
    grants_idx = html.find('data-panel="grants"')
    mcp_idx = html.find('data-panel="mcp"')
    assert cost_idx < grants_idx < mcp_idx


def test_index_html_has_grants_panel_section():
    html = _read(_STATIC / "index.html")
    assert 'id="panel-grants"' in html
    assert 'id="grants-list"' in html
    assert 'id="grants-refresh"' in html
    assert 'id="grants-clear-all"' in html


def test_index_html_grants_section_mentions_path():
    """Show users where the file lives — debugging / hand-edit
    sometimes makes more sense than the UI."""
    html = _read(_STATIC / "index.html")
    start = html.find('id="panel-grants"')
    end = html.find("</section>", start)
    section = html[start:end]
    assert "approvals.json" in section


def test_app_js_registers_grants_panel():
    js = _read(_STATIC / "app.js")
    assert "registerPanel('grants'" in js


def test_app_js_grants_panel_calls_endpoints():
    js = _read(_STATIC / "app.js")
    region_start = js.find("registerPanel('grants'")
    region = js[region_start:region_start + 4000]
    assert "/api/grants" in region
    assert "/api/grants/revoke" in region
    assert "/api/grants/clear" in region


def test_app_js_grants_revoke_confirms():
    """v1.31.2: revoke + clear are destructive — confirm() prompt."""
    js = _read(_STATIC / "app.js")
    region_start = js.find("registerPanel('grants'")
    region = js[region_start:region_start + 4000]
    assert "confirm(" in region


def test_app_js_grants_uses_risk_tag_class():
    """Reuse the same risk-tag styling the approval modal uses."""
    js = _read(_STATIC / "app.js")
    region_start = js.find("registerPanel('grants'")
    region = js[region_start:region_start + 4000]
    assert "risk-" in region


def test_app_js_grants_reloads_after_mutation():
    """After revoke / clear succeeds, the panel must re-fetch
    so the UI reflects the new state."""
    js = _read(_STATIC / "app.js")
    region_start = js.find("registerPanel('grants'")
    region = js[region_start:region_start + 4000]
    # The reload pattern is `await load()` after a successful mutation.
    assert "await load()" in region
