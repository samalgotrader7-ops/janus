"""Tests for v1.31.0 — web Skills proposals panel.

Surfaces v1.28.0 ``skill_proposer`` suggestions in the web SPA: a
new block at the top of the existing /skills panel lists offerable
recurring patterns, with [draft] and [decline] buttons that map to
new POST endpoints.

Mutating endpoints (``/propose`` and ``/decline``) gate via
``_gate_post`` (auth + rate-limit + CSRF) and write to web_audit —
same pattern as the existing /api/skills/{name}/promote.

Source-pin and static-asset tests cover the shape; an integration-
style smoke test exercises the GET endpoint via the FastAPI app.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from janus import skill_proposer
from janus.gateways import web as web_mod


_STATIC = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================
# Static asset additions (HTML + JS + CSS reuse)
# ============================================================


def test_index_html_has_suggestions_block():
    html = _read(_STATIC / "index.html")
    assert 'id="skills-suggestions-block"' in html
    assert 'id="skills-suggestions-list"' in html
    assert 'id="skills-suggestions-count"' in html


def test_index_html_suggestions_starts_hidden():
    """JS only un-hides the block when patterns are returned."""
    html = _read(_STATIC / "index.html")
    # find the suggestions block
    start = html.find('id="skills-suggestions-block"')
    assert start != -1
    end = html.find(">", start)
    tag = html[start:end + 1]
    assert "display:none" in tag.replace(" ", "")


def test_index_html_suggestions_within_skills_panel():
    """The new block must live INSIDE panel-skills, not as a peer
    section — we don't want a new sidebar entry."""
    html = _read(_STATIC / "index.html")
    panel_start = html.find('id="panel-skills"')
    panel_end = html.find("</section>", panel_start)
    section = html[panel_start:panel_end]
    assert 'id="skills-suggestions-block"' in section


def test_app_js_load_skill_suggestions_function():
    js = _read(_STATIC / "app.js")
    assert "loadSkillSuggestions" in js


def test_app_js_calls_suggestions_endpoint():
    js = _read(_STATIC / "app.js")
    assert "/api/skills/suggestions" in js


def test_app_js_calls_propose_endpoint_on_click():
    js = _read(_STATIC / "app.js")
    assert "/propose" in js
    # POST shape — same wrapper as elsewhere
    assert "method: 'POST'" in js


def test_app_js_calls_decline_endpoint_on_click():
    js = _read(_STATIC / "app.js")
    assert "/decline" in js


def test_app_js_remounts_panel_after_draft():
    """A successful draft must refresh the panel so the new
    quarantined skill appears in the installed list and the
    pattern leaves the suggestions list."""
    js = _read(_STATIC / "app.js")
    region_start = js.find("loadSkillSuggestions")
    region = js[region_start:region_start + 4000]
    assert "panels.skills.mount" in region


def test_app_js_skills_panel_loads_in_parallel():
    """Suggestions + installed-list fetched concurrently for
    perceived speed (Promise.all)."""
    js = _read(_STATIC / "app.js")
    region_start = js.find("registerPanel('skills'")
    region = js[region_start:region_start + 4000]
    assert "Promise.all" in region


# ============================================================
# Web routes — source pins + signature checks
# ============================================================


def test_web_module_defines_three_new_routes():
    src = inspect.getsource(web_mod)
    assert "/api/skills/suggestions" in src
    assert "/api/skills/suggestions/{pattern_id}/propose" in src
    assert "/api/skills/suggestions/{pattern_id}/decline" in src


def test_web_propose_route_is_mutating():
    """v1.31.0 source pin: propose uses _gate_post (auth + rate-
    limit + CSRF + audit), not _gate_get."""
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_skill_propose")
    assert region_start != -1
    region = src[region_start:region_start + 2000]
    assert "_gate_post" in region
    assert "web_audit.mutate" in region


def test_web_decline_route_is_mutating():
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_skill_decline")
    assert region_start != -1
    region = src[region_start:region_start + 2000]
    assert "_gate_post" in region
    assert "web_audit.mutate" in region


def test_web_suggestions_route_is_read_only():
    """GET — no mutation, but DOES mark patterns as offered
    (cooldown side-effect on the proposer state file)."""
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_skill_suggestions")
    region = src[region_start:region_start + 2000]
    assert "_gate_get" in region
    # Side-effect: mark_offered called
    assert "mark_offered" in region


def test_web_suggestions_caps_at_top_10():
    """Parity with cli_rich /skills suggestions which also caps at 10."""
    src = inspect.getsource(web_mod)
    region_start = src.find("def api_skill_suggestions")
    region = src[region_start:region_start + 2000]
    assert "[:10]" in region


# ============================================================
# Behavioral test — exercise the endpoints via FastAPI app
# ============================================================


def _build_test_client(monkeypatch, tmp_path):
    """Helper: spin up a TestClient with auth disabled and a temp
    JANUS_HOME so we don't touch the user's real state."""
    from janus import config as cfg
    monkeypatch.setattr(cfg, "HOME", tmp_path)
    monkeypatch.setattr(cfg, "WORKSPACE", str(tmp_path))
    # web_auth disabling: we mock _check_auth to always pass.
    monkeypatch.setattr(
        web_mod, "_check_auth",
        lambda req: ("test-sid", None),
    )
    # CSRF + rate-limit pass-throughs.
    monkeypatch.setattr(web_mod, "_check_csrf", lambda req, sid: True)
    from janus.gateways import web_auth
    monkeypatch.setattr(
        web_auth, "rate_limit_take",
        lambda sid, kind: (True, 0.0),
    )
    from fastapi.testclient import TestClient
    app = web_mod._build_app()
    return TestClient(app)


def test_get_suggestions_empty(monkeypatch, tmp_path):
    """No detected patterns → empty list, total=0."""
    monkeypatch.setattr(skill_proposer, "list_offerable", lambda: [])
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.get("/api/skills/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert body["patterns"] == []
    assert body["total"] == 0
    assert "cooldown_days" in body


def test_get_suggestions_returns_patterns(monkeypatch, tmp_path):
    fake = [
        skill_proposer.Pattern(
            id="seq-fs-read-fs-edit",
            kind="repeated_tool_sequence",
            description="fs_read then fs_edit ran 5 times",
            occurrences=5,
        ),
        skill_proposer.Pattern(
            id="file-foo-py",
            kind="repeated_file",
            description="foo.py touched 6 times",
            occurrences=6,
        ),
    ]
    monkeypatch.setattr(skill_proposer, "list_offerable", lambda: fake)
    monkeypatch.setattr(skill_proposer, "mark_offered", lambda pid: None)
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.get("/api/skills/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["patterns"]) == 2
    assert body["patterns"][0]["id"] == "seq-fs-read-fs-edit"
    assert body["patterns"][0]["occurrences"] == 5
    assert body["patterns"][1]["kind"] == "repeated_file"


def test_get_suggestions_marks_offered(monkeypatch, tmp_path):
    """Side effect: each surfaced pattern hits mark_offered so the
    cooldown timer respects 'user has now seen this'."""
    fake = [
        skill_proposer.Pattern(
            id=f"p-{i}", kind="repeated_file",
            description=f"pattern {i}", occurrences=4,
        )
        for i in range(3)
    ]
    monkeypatch.setattr(skill_proposer, "list_offerable", lambda: fake)
    seen: list[str] = []
    monkeypatch.setattr(
        skill_proposer, "mark_offered",
        lambda pid: seen.append(pid),
    )
    client = _build_test_client(monkeypatch, tmp_path)
    client.get("/api/skills/suggestions")
    assert seen == ["p-0", "p-1", "p-2"]


def test_post_decline_marks_declined(monkeypatch, tmp_path):
    declined: list[str] = []
    monkeypatch.setattr(
        skill_proposer, "mark_declined",
        lambda pid: declined.append(pid),
    )
    # Audit no-op so we don't have to set up the audit log.
    from janus.gateways import web_audit
    monkeypatch.setattr(web_audit, "mutate", lambda *a, **k: None)
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/skills/suggestions/seq-foo/decline",
        headers={"X-CSRF-Token": "any"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert declined == ["seq-foo"]


def test_post_propose_404_when_pattern_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(skill_proposer, "detect", lambda: [])
    from janus.gateways import web_audit
    monkeypatch.setattr(web_audit, "mutate", lambda *a, **k: None)
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/skills/suggestions/seq-unknown/propose",
        headers={"X-CSRF-Token": "any"},
    )
    assert r.status_code == 404
    assert "no pattern with id" in r.json()["error"]


def test_post_propose_drafts_known_pattern(monkeypatch, tmp_path):
    target = skill_proposer.Pattern(
        id="file-bar-py", kind="repeated_file",
        description="bar.py touched 4 times", occurrences=4,
    )
    monkeypatch.setattr(skill_proposer, "detect", lambda: [target])
    drafted: list[str] = []

    def fake_draft(pattern, *, current_trace=None):
        drafted.append(pattern.id)
        return tmp_path / "skills" / "auto-bar-py.md"

    monkeypatch.setattr(skill_proposer, "draft_skill", fake_draft)
    from janus.gateways import web_audit
    monkeypatch.setattr(web_audit, "mutate", lambda *a, **k: None)
    client = _build_test_client(monkeypatch, tmp_path)
    r = client.post(
        "/api/skills/suggestions/file-bar-py/propose",
        headers={"X-CSRF-Token": "any"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["state"] == "quarantined"
    assert body["name"] == "auto-bar-py"
    assert drafted == ["file-bar-py"]
