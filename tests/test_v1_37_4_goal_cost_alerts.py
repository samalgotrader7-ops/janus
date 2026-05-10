"""Tests for v1.37.4 — /goal cost tracking + 50/80/100% alerts (Phase 10.1.4).

Coverage:
  * GoalState gains cost_usd + budget_alerts_fired (back-compat load)
  * after_turn() accumulates cost from cost.turn_stats()
  * 50/80/100% threshold alerts fire ONCE each
  * AutoContinueDecision carries budget_alert + cost_usd
  * /goal status shows cost
  * web /chat response includes budget_alert + cost_usd
  * web /api/goal/status returns cost_usd + progress_ratio
"""

from __future__ import annotations

import pytest

from janus import goals, goal_loop


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    yield


def _patch_judge(monkeypatch, *, achieved=False, next_step="next"):
    monkeypatch.setattr(
        goal_loop, "run_judge",
        lambda goal_text, last_response: goal_loop.JudgeResult(
            achieved=achieved, reason="ok", next_step=next_step,
        ),
    )


def _patch_cost_per_turn(monkeypatch, usd: float):
    """Pin a constant cost for each cost.turn_stats() call so tests
    are deterministic."""
    from janus import cost
    fake_stats = cost.TokenStats(prompt_tokens=10, completion_tokens=20, calls=1, usd=usd)
    monkeypatch.setattr(cost, "turn_stats", lambda: fake_stats)


# ---------- GoalState back-compat ----------


def test_goal_state_defaults():
    g = goals.GoalState(text="x")
    assert g.cost_usd == 0.0
    assert g.budget_alerts_fired == []


def test_goal_state_load_back_compat_no_cost(tmp_path, monkeypatch):
    """Pin: a v1.37.3 goal file (no cost_usd / budget_alerts_fired)
    still loads with sensible defaults — back-compat for users who
    set goals on v1.37.x then upgrade."""
    import json
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    p = tmp_path / "goals" / "cli_rich.json"
    p.parent.mkdir()
    p.write_text(json.dumps({
        "text": "x",
        "status": "active",
        "turn_budget": 50,
        "turns_used": 5,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "paused_at": None,
        "recent_response_hashes": [],
    }))
    g = goals.load("cli_rich")
    assert g.cost_usd == 0.0
    assert g.budget_alerts_fired == []


def test_progress_ratio():
    g = goals.GoalState(text="x", turn_budget=10, turns_used=4)
    assert g.progress_ratio() == 0.4
    g.turns_used = 10
    assert g.progress_ratio() == 1.0


def test_progress_ratio_zero_budget():
    g = goals.GoalState(text="x", turn_budget=0)
    assert g.progress_ratio() == 0.0


# ---------- cost accumulation ----------


def test_cost_accumulates_per_turn(monkeypatch):
    goals.set_goal("cli_rich", "x", turn_budget=10)
    _patch_judge(monkeypatch)
    _patch_cost_per_turn(monkeypatch, usd=0.001)
    goal_loop.after_turn("cli_rich", "alpha")
    g = goals.load("cli_rich")
    assert g.cost_usd == pytest.approx(0.001, abs=1e-9)
    goal_loop.after_turn("cli_rich", "beta")
    g = goals.load("cli_rich")
    assert g.cost_usd == pytest.approx(0.002, abs=1e-9)


def test_decision_includes_cost_usd(monkeypatch):
    goals.set_goal("cli_rich", "x", turn_budget=10)
    _patch_judge(monkeypatch)
    _patch_cost_per_turn(monkeypatch, usd=0.005)
    d = goal_loop.after_turn("cli_rich", "out")
    assert d.cost_usd == pytest.approx(0.005, abs=1e-9)


# ---------- 50/80/100% alerts ----------


def test_50pct_alert_fires_at_50_pct(monkeypatch):
    goals.set_goal("cli_rich", "x", turn_budget=10)
    _patch_judge(monkeypatch)
    _patch_cost_per_turn(monkeypatch, usd=0.0)
    # turns 1-4 → ratios 0.1..0.4 — no alerts.
    # Use unique responses each turn or cycle detector pauses by turn 3.
    for i in range(4):
        d = goal_loop.after_turn("cli_rich", f"out-{i}")
        assert d.budget_alert is None
    # turn 5 → ratio 0.5 → 50% alert
    d = goal_loop.after_turn("cli_rich", "out-5")
    assert d.budget_alert == 0.5


def test_each_threshold_fires_once(monkeypatch):
    """Pin: 50% only fires once — not on subsequent turns above 50%
    but below 80%."""
    goals.set_goal("cli_rich", "x", turn_budget=10)
    _patch_judge(monkeypatch)
    _patch_cost_per_turn(monkeypatch, usd=0.0)
    alerts = []
    for _ in range(7):
        d = goal_loop.after_turn("cli_rich", f"out-{_}")
        if d.budget_alert is not None:
            alerts.append(d.budget_alert)
    # turn 5 → 50% (0.5), turn 8 → 80% (0.8)
    # but we only ran 7 turns, so just 50%
    assert alerts == [0.5]


def test_80pct_alert_fires(monkeypatch):
    goals.set_goal("cli_rich", "x", turn_budget=10)
    _patch_judge(monkeypatch)
    _patch_cost_per_turn(monkeypatch, usd=0.0)
    alerts = []
    for _ in range(8):
        d = goal_loop.after_turn("cli_rich", f"out-{_}")
        if d.budget_alert is not None:
            alerts.append(d.budget_alert)
    # 50% at turn 5, 80% at turn 8
    assert alerts == [0.5, 0.8]


def test_100pct_alert_fires_with_budget_pause(monkeypatch):
    goals.set_goal("cli_rich", "x", turn_budget=10)
    _patch_judge(monkeypatch)
    _patch_cost_per_turn(monkeypatch, usd=0.0)
    final_decision = None
    for _ in range(10):
        final_decision = goal_loop.after_turn("cli_rich", f"out-{_}")
    # Last turn pushed turns_used to 10 → 100% → both 1.0 alert
    # AND budget exhausted pause
    assert final_decision.paused is True
    assert final_decision.budget_exhausted is True
    assert final_decision.budget_alert == 1.0


def test_alerts_persist_across_loads(monkeypatch):
    """Pin: budget_alerts_fired is on disk so a process restart
    doesn't re-fire the same threshold."""
    goals.set_goal("cli_rich", "x", turn_budget=10)
    _patch_judge(monkeypatch)
    _patch_cost_per_turn(monkeypatch, usd=0.0)
    for _ in range(5):
        goal_loop.after_turn("cli_rich", f"out-{_}")
    g = goals.load("cli_rich")
    assert 0.5 in g.budget_alerts_fired
    # Subsequent turn (still under 80%) should NOT re-fire 50%
    d = goal_loop.after_turn("cli_rich", "out6")
    assert d.budget_alert is None


# ---------- format_status with cost ----------


def test_format_status_includes_cost():
    g = goals.set_goal("cli_rich", "x", turn_budget=10)
    g.cost_usd = 0.0123
    out = goals.format_status(g)
    assert "$0.0123" in out


def test_format_status_omits_cost_when_zero():
    g = goals.set_goal("cli_rich", "x", turn_budget=10)
    out = goals.format_status(g)
    assert "spent" not in out.lower()


# ---------- web cross-surface ----------


_HAS_FASTAPI = True
try:
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
except ImportError:
    _HAS_FASTAPI = False


def _logged_in_client(app):
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    csrf = r.json()["csrf_token"]
    return c, csrf


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_goal_status_includes_cost(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c, _csrf = _logged_in_client(app)
    g = goals.set_goal("web:s1", "x", turn_budget=10)
    g.cost_usd = 0.005
    g.turns_used = 5
    goals.save("web:s1", g)
    r = c.get("/api/goal/status?session_id=s1")
    body = r.json()["goal"]
    assert body["cost_usd"] == 0.005
    assert body["progress_ratio"] == 0.5


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_response_includes_budget_alert(janus_home, monkeypatch):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()

    from janus import app as janus_app, goal_loop
    monkeypatch.setattr(janus_app, "run_turn", lambda **kw: ("out", []))
    monkeypatch.setattr(
        goal_loop, "run_judge",
        lambda goal_text, last_response: goal_loop.JudgeResult(
            achieved=False, reason="wip", next_step="next",
        ),
    )
    _patch_cost_per_turn(monkeypatch, usd=0.001)

    app = web_mod._build_app()
    c, csrf = _logged_in_client(app)
    sid = "alert-sess"
    # Pre-seed at 4/10 turns; one more push crosses 50%
    g = goals.set_goal(f"web:{sid}", "x", turn_budget=10)
    g.turns_used = 4
    goals.save(f"web:{sid}", g)
    r = c.post(
        "/chat",
        headers={"X-CSRF-Token": csrf},
        json={"request": "go", "session_id": sid},
    )
    body = r.json()
    assert "goal" in body
    assert body["goal"].get("budget_alert") == 0.5
    assert body["goal"].get("cost_usd") == 0.001


# ---------- version ----------


def test_version_bumped_to_1_37_4():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 37, 4)
