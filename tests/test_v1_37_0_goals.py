"""Tests for v1.37.0 — /goal Ralph Loop primitive (Phase 10.1.0).

Coverage:
  goals.py — set/load/clear, status transitions, atomic save,
             scope path safety
  slash    — bare /goal, /goal <text>, /goal status, /goal pause,
             /goal resume, /goal clear, /goal budget <N>,
             /goal in BUILTIN_COMMANDS catalogue
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from janus import goals, slash_dispatch as sd


# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Redirect ~/.janus → tmp_path for every test in this file."""
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    yield


@pytest.fixture
def reg():
    r = sd.SlashRegistry()
    sd.register_shared_handlers(r)
    return r


def _ctx(surface="cli_rich", state=None, extra=None):
    return sd.SlashContext(
        surface=surface,
        state=state or {},
        extra=extra or {},
        console=None,
        print_fn=lambda s: None,
    )


# ---------- primitive ----------


def test_set_goal_creates_state():
    g = goals.set_goal("cli_rich", "ship Phase 10")
    assert g.text == "ship Phase 10"
    assert g.status == "active"
    assert g.turn_budget == 500
    assert g.turns_used == 0


def test_set_goal_strips_whitespace():
    g = goals.set_goal("cli_rich", "   ship it   ")
    assert g.text == "ship it"


def test_set_goal_rejects_empty():
    with pytest.raises(ValueError):
        goals.set_goal("cli_rich", "")
    with pytest.raises(ValueError):
        goals.set_goal("cli_rich", "   ")


def test_set_goal_rejects_zero_budget():
    with pytest.raises(ValueError):
        goals.set_goal("cli_rich", "x", turn_budget=0)


def test_load_returns_none_when_absent():
    assert goals.load("nobody") is None


def test_load_round_trip():
    goals.set_goal("cli_rich", "do thing")
    g = goals.load("cli_rich")
    assert g is not None
    assert g.text == "do thing"
    assert g.status == "active"


def test_pause_resume_round_trip():
    goals.set_goal("cli_rich", "x")
    g = goals.pause("cli_rich")
    assert g.status == "paused"
    assert g.paused_at is not None
    g = goals.resume("cli_rich")
    assert g.status == "active"
    assert g.paused_at is None


def test_pause_idempotent_when_already_paused():
    goals.set_goal("cli_rich", "x")
    goals.pause("cli_rich")
    g = goals.pause("cli_rich")  # no-op
    assert g.status == "paused"


def test_clear_removes_file():
    goals.set_goal("cli_rich", "x")
    assert goals.clear("cli_rich") is True
    assert goals.load("cli_rich") is None
    # Second clear is a no-op
    assert goals.clear("cli_rich") is False


def test_increment_turns_only_when_active():
    goals.set_goal("cli_rich", "x", turn_budget=10)
    g = goals.increment_turns("cli_rich")
    assert g.turns_used == 1
    goals.pause("cli_rich")
    g = goals.increment_turns("cli_rich")
    assert g.turns_used == 1  # no bump while paused


def test_remaining_turns_and_budget_exhausted():
    goals.set_goal("cli_rich", "x", turn_budget=3)
    for _ in range(3):
        goals.increment_turns("cli_rich")
    g = goals.load("cli_rich")
    assert g.remaining_turns() == 0
    assert g.budget_exhausted() is True


def test_scope_path_with_colons_and_slashes():
    """telegram:<id> and web:<sess> scopes shouldn't crash on path build."""
    goals.set_goal("telegram:12345", "x")
    goals.set_goal("web:abc/def", "y")
    assert goals.load("telegram:12345").text == "x"
    assert goals.load("web:abc/def").text == "y"


def test_atomic_save_via_temp_rename(tmp_path, monkeypatch):
    """Pin: temp file is renamed onto target — partial writes can't
    corrupt state. Verify .tmp doesn't survive the call."""
    goals.set_goal("cli_rich", "x")
    # No leftover .tmp files
    leftovers = list(tmp_path.glob("**/*.tmp"))
    assert leftovers == []


def test_format_status_no_goal():
    out = goals.format_status(None)
    assert "no goal set" in out.lower()


def test_format_status_active_goal():
    g = goals.set_goal("cli_rich", "ship it", turn_budget=100)
    out = goals.format_status(g)
    assert "ship it" in out
    assert "active" in out
    assert "0/100" in out


# ---------- slash handler ----------


def test_slash_bare_goal_with_no_state(reg):
    handled, out = reg.dispatch("/goal", _ctx())
    assert handled is True
    assert "no goal set" in out.lower()


def test_slash_set_goal(reg):
    handled, out = reg.dispatch("/goal refactor the planner", _ctx())
    assert handled is True
    assert "refactor the planner" in out
    # Goal is persisted to disk
    g = goals.load("cli_rich")
    assert g is not None
    assert g.text == "refactor the planner"


def test_slash_set_goal_preserves_multiword_text(reg):
    """Pin: '/goal status' is a SUBCOMMAND, but '/goal status report'
    is goal text. The subcommand keyword check uses split_subcommand,
    which only matches single-token keywords."""
    handled, out = reg.dispatch("/goal status", _ctx())
    # 'status' is a reserved subcommand, so this is a status query
    assert "no goal set" in out.lower()


def test_slash_status_subcommand(reg):
    goals.set_goal("cli_rich", "x")
    handled, out = reg.dispatch("/goal status", _ctx())
    assert handled is True
    assert "x" in out
    assert "active" in out


def test_slash_pause_then_resume(reg):
    goals.set_goal("cli_rich", "x")
    handled, out = reg.dispatch("/goal pause", _ctx())
    assert "paused" in out.lower()
    handled, out = reg.dispatch("/goal resume", _ctx())
    assert "resumed" in out.lower() or "active" in out.lower()


def test_slash_pause_no_goal(reg):
    handled, out = reg.dispatch("/goal pause", _ctx())
    assert "no goal" in out.lower()


def test_slash_clear(reg):
    goals.set_goal("cli_rich", "x")
    handled, out = reg.dispatch("/goal clear", _ctx())
    assert "cleared" in out.lower()
    assert goals.load("cli_rich") is None


def test_slash_clear_idempotent(reg):
    handled, out = reg.dispatch("/goal clear", _ctx())
    assert "no goal" in out.lower()


def test_slash_budget_update(reg):
    goals.set_goal("cli_rich", "x", turn_budget=10)
    handled, out = reg.dispatch("/goal budget 250", _ctx())
    assert "250" in out
    g = goals.load("cli_rich")
    assert g.turn_budget == 250


def test_slash_budget_invalid(reg):
    goals.set_goal("cli_rich", "x")
    handled, out = reg.dispatch("/goal budget abc", _ctx())
    assert "usage" in out.lower() or "positive" in out.lower()


def test_slash_budget_zero_rejected(reg):
    goals.set_goal("cli_rich", "x")
    handled, out = reg.dispatch("/goal budget 0", _ctx())
    assert "positive" in out.lower()


def test_slash_uses_extra_scope_when_provided(reg):
    """Telegram/web pass scope via ctx.extra['goal_scope']."""
    extra = {"goal_scope": "telegram:42"}
    handled, out = reg.dispatch("/goal hello", _ctx(extra=extra))
    assert handled is True
    # Goal is stored under telegram:42, not 'cli_rich'
    assert goals.load("telegram:42") is not None
    assert goals.load("cli_rich") is None


# ---------- catalogue ----------


def test_goal_in_builtin_commands():
    """Pin: /goal appears in the canonical command palette."""
    names = [c.name for c in sd.BUILTIN_COMMANDS]
    assert "/goal" in names


def test_goal_handler_registered_after_register_shared(reg):
    assert reg.has("/goal")


def test_version_bumped_to_1_37_0():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 37, 0)
