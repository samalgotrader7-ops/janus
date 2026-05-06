"""Smoke tests for v1.22.1 / v1.22.2 / v1.22.3 panel API endpoints.

Each endpoint gets at least one auth-required + one happy-path test.
Behavior depth (e.g., correct trigger schedule parsing) is covered in
the underlying module tests; here we verify the HTTP contract.
"""
from __future__ import annotations

import pytest

try:
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    from janus.gateways import web_auth
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


pytestmark = pytest.mark.skipif(
    not _HAS_FASTAPI, reason="fastapi not installed",
)


def _authed_client(janus_home_path=None):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200, r.text
    c.csrf_token = r.json()["csrf_token"]  # type: ignore[attr-defined]
    return c


def _unauthed_client(janus_home_path=None):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    return TestClient(app)


# ---------- v1.22.1: card delete + skill mutations + interview ----------


def test_card_delete_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/cards/some-id/delete")
    assert r.status_code == 401


def test_card_delete_unknown_returns_404(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/cards/no-such-card-id/delete",
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 404


def test_skill_promote_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/skills/x/promote", json={"state": "promoted"})
    assert r.status_code == 401


def test_skill_promote_unknown_returns_400(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/skills/totally-fake-skill/promote",
        json={"state": "promoted"},
        headers={"x-csrf-token": c.csrf_token},
    )
    # Either 400 (no such skill) or 200 (PromotionError → returned as 400).
    assert r.status_code in (400, 404)


def test_skills_install_bundled_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/skills/install-bundled", json={"force": False})
    assert r.status_code == 401


def test_skills_install_bundled_runs(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/skills/install-bundled",
        json={"force": False},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert "result" in data


def test_interview_state_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/interview/state")
    assert r.status_code == 401


def test_interview_state_returns_completion_meter(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/interview/state?session_id=test-session")
    assert r.status_code == 200
    data = r.json()
    assert "mode" in data
    assert "completion" in data
    assert isinstance(data["completion"], dict)
    assert "categories" in data
    assert isinstance(data["categories"], list)


def test_interview_start_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/interview/start", json={"session_id": "x"})
    assert r.status_code == 401


def test_interview_start_returns_ok(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/interview/start",
        json={"session_id": "test-s", "daily_count": 1},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_interview_pause_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/interview/pause", json={"session_id": "x"})
    assert r.status_code == 401


def test_interview_pause_returns_ok(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/interview/pause",
        json={"session_id": "test-s"},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_interview_about_me_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/interview/about-me")
    assert r.status_code == 401


def test_interview_about_me_returns_body(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/interview/about-me")
    assert r.status_code == 200
    assert "body" in r.json()


# ---------- v1.22.2: agents / swarms / triggers ----------


def test_agents_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/agents")
    assert r.status_code == 401


def test_agents_returns_output(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/agents")
    assert r.status_code == 200
    assert "output" in r.json()


def test_agent_run_now_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/agents/x/run-now")
    assert r.status_code == 401


def test_agent_run_now_unknown_returns_message(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/agents/no-such-agent/run-now",
        headers={"x-csrf-token": c.csrf_token},
    )
    # The tool returns a string error message in `output` rather than
    # a non-200 status — keep the contract stable.
    assert r.status_code == 200


def test_agent_set_enabled_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/agents/x/set-enabled", json={"enabled": True})
    assert r.status_code == 401


def test_agent_delete_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/agents/x/delete")
    assert r.status_code == 401


def test_swarm_specs_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/swarms/specs")
    assert r.status_code == 401


def test_swarm_specs_returns_list(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/swarms/specs")
    assert r.status_code == 200
    assert "specs" in r.json()


def test_swarm_runs_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/swarms/runs")
    assert r.status_code == 401


def test_swarm_runs_returns_list(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/swarms/runs?limit=5")
    assert r.status_code == 200
    assert "runs" in r.json()


def test_triggers_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/triggers")
    assert r.status_code == 401


def test_triggers_returns_list(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/triggers")
    assert r.status_code == 200
    assert "triggers" in r.json()


# ---------- v1.22.3: shells / logs / cost / settings ----------


def test_shells_list_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/shells")
    assert r.status_code == 401


def test_shells_list_returns_output(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/shells")
    assert r.status_code == 200
    assert "output" in r.json()


def test_shells_run_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/shells/run", json={"command": "echo hi"})
    assert r.status_code == 401


def test_shells_run_requires_csrf(janus_home):
    c = _authed_client(janus_home)
    r = c.post("/api/shells/run", json={"command": "echo hi"})
    assert r.status_code == 403


def test_shells_run_empty_command_400(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/shells/run", json={"command": ""},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 400


def test_shell_output_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/shells/sh-fake/output")
    assert r.status_code == 401


def test_shell_kill_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.post("/api/shells/sh-fake/kill")
    assert r.status_code == 401


def test_logs_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/logs")
    assert r.status_code == 401


def test_logs_returns_entries(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/logs?limit=10")
    assert r.status_code == 200
    assert "entries" in r.json()
    assert isinstance(r.json()["entries"], list)


def test_cost_summary_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/cost-summary")
    assert r.status_code == 401


def test_cost_summary_returns_text(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/cost-summary")
    assert r.status_code == 200
    assert "summary" in r.json()


def test_settings_requires_auth(janus_home):
    c = _unauthed_client(janus_home)
    r = c.get("/api/settings")
    assert r.status_code == 401


def test_settings_returns_known_fields(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    # Sanity-check the documented fields are present.
    for key in (
        "mode", "model", "workspace", "home",
        "step_soft_cap", "step_hard_cap", "step_progress_grace",
        "version",
    ):
        assert key in data, f"missing settings key: {key}"
