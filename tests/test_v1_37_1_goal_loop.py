"""Tests for v1.37.1 — /goal judge + auto-continue loop (Phase 10.1.1).

Coverage:
  * judge prompt building + JSON parsing tolerance
  * after_turn() — achieved / continue / paused / cycle / budget paths
  * cycle detection: 3 identical hashes = pause
  * budget exhaustion: turns_used >= turn_budget = pause
  * judge model errors fall back to a safe default
  * plan-mode auto-leave on /goal set (slash handler)
"""

from __future__ import annotations

import pytest

from janus import goals, goal_loop, slash_dispatch as sd, permissions


# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    yield


def _ctx(surface="cli_rich", state=None, extra=None):
    return sd.SlashContext(
        surface=surface,
        state=state or {},
        extra=extra or {},
        console=None,
        print_fn=lambda s: None,
    )


# ---------- judge response parsing ----------


def test_parse_judge_pure_json():
    raw = '{"achieved": true, "reason": "did it", "next_step": ""}'
    r = goal_loop._parse_judge_response(raw)
    assert r is not None
    assert r.achieved is True
    assert r.reason == "did it"


def test_parse_judge_markdown_fenced():
    raw = '```json\n{"achieved": false, "reason": "wip", "next_step": "edit X"}\n```'
    r = goal_loop._parse_judge_response(raw)
    assert r is not None
    assert r.achieved is False
    assert "edit X" in r.next_step


def test_parse_judge_with_leading_prose():
    raw = (
        "Sure, here's my evaluation:\n\n"
        '{"achieved": false, "reason": "needs more", "next_step": "do thing"}'
    )
    r = goal_loop._parse_judge_response(raw)
    assert r is not None
    assert r.achieved is False


def test_parse_judge_unparseable_returns_none():
    assert goal_loop._parse_judge_response("not json at all") is None
    assert goal_loop._parse_judge_response("") is None
    assert goal_loop._parse_judge_response("   ") is None


def test_parse_judge_non_dict_returns_none():
    raw = '["achieved", "reason"]'
    assert goal_loop._parse_judge_response(raw) is None


# ---------- cycle detection ----------


def test_response_hash_normalizes_whitespace():
    h1 = goal_loop._response_hash("hello   world\n\n")
    h2 = goal_loop._response_hash("Hello world")
    assert h1 == h2


def test_response_hash_empty():
    assert goal_loop._response_hash("") == ""
    assert goal_loop._response_hash("   ") == goal_loop._response_hash("")


def test_is_cycle_needs_3_identical():
    h = "abcd" * 4
    assert goal_loop._is_cycle([], h) is False
    assert goal_loop._is_cycle([h], h) is False
    assert goal_loop._is_cycle([h, h], h) is True
    assert goal_loop._is_cycle(["other", h], h) is False


# ---------- after_turn paths ----------


def _patch_judge(monkeypatch, *, achieved, reason="", next_step=""):
    """Replace run_judge with a fixed verdict for the test."""
    monkeypatch.setattr(
        goal_loop,
        "run_judge",
        lambda goal_text, last_response: goal_loop.JudgeResult(
            achieved=achieved, reason=reason, next_step=next_step,
        ),
    )


def test_after_turn_no_active_goal_returns_inert(monkeypatch):
    _patch_judge(monkeypatch, achieved=False)
    d = goal_loop.after_turn("cli_rich", "anything")
    assert d.next_prompt is None
    assert d.achieved is False
    assert d.paused is False


def test_after_turn_paused_goal_returns_inert(monkeypatch):
    goals.set_goal("cli_rich", "x")
    goals.pause("cli_rich")
    _patch_judge(monkeypatch, achieved=False)
    d = goal_loop.after_turn("cli_rich", "out")
    assert d.next_prompt is None


def test_after_turn_achieved_marks_done(monkeypatch):
    goals.set_goal("cli_rich", "ship it")
    _patch_judge(monkeypatch, achieved=True, reason="done")
    d = goal_loop.after_turn("cli_rich", "✓ committed")
    assert d.achieved is True
    g = goals.load("cli_rich")
    assert g.status == "done"


def test_after_turn_continues_with_judge_hint(monkeypatch):
    goals.set_goal("cli_rich", "x")
    _patch_judge(
        monkeypatch, achieved=False,
        next_step="run the failing test",
    )
    d = goal_loop.after_turn("cli_rich", "wrote a fix")
    assert d.next_prompt == "run the failing test"
    assert d.achieved is False
    assert d.paused is False


def test_after_turn_continues_with_default_when_judge_returns_empty_hint(monkeypatch):
    goals.set_goal("cli_rich", "x")
    _patch_judge(monkeypatch, achieved=False, next_step="")
    d = goal_loop.after_turn("cli_rich", "out")
    assert d.next_prompt is not None
    assert "x" in d.next_prompt  # falls back to a generic continue mentioning the goal


def test_after_turn_increments_turn_counter(monkeypatch):
    goals.set_goal("cli_rich", "x", turn_budget=10)
    _patch_judge(monkeypatch, achieved=False, next_step="next")
    goal_loop.after_turn("cli_rich", "out1")
    goal_loop.after_turn("cli_rich", "out2")
    g = goals.load("cli_rich")
    assert g.turns_used == 2


def test_after_turn_budget_exhausted_pauses(monkeypatch):
    goals.set_goal("cli_rich", "x", turn_budget=2)
    _patch_judge(monkeypatch, achieved=False, next_step="next")
    goal_loop.after_turn("cli_rich", "out1")
    d = goal_loop.after_turn("cli_rich", "out2")  # this pushes turns_used to 2 → exhausted
    assert d.paused is True
    assert d.budget_exhausted is True
    g = goals.load("cli_rich")
    assert g.status == "paused"


def test_after_turn_cycle_pauses(monkeypatch):
    goals.set_goal("cli_rich", "x")
    _patch_judge(monkeypatch, achieved=False, next_step="next")
    # 3 identical responses → cycle on the 3rd
    goal_loop.after_turn("cli_rich", "same response")
    goal_loop.after_turn("cli_rich", "same response")
    d = goal_loop.after_turn("cli_rich", "same response")
    assert d.paused is True
    assert d.cycle_detected is True
    g = goals.load("cli_rich")
    assert g.status == "paused"


def test_after_turn_different_responses_no_cycle(monkeypatch):
    goals.set_goal("cli_rich", "x")
    _patch_judge(monkeypatch, achieved=False, next_step="next")
    goal_loop.after_turn("cli_rich", "first")
    goal_loop.after_turn("cli_rich", "second")
    d = goal_loop.after_turn("cli_rich", "third")
    assert d.paused is False
    assert d.cycle_detected is False
    assert d.next_prompt == "next"


def test_judge_failure_falls_back_to_safe_default(monkeypatch):
    """Pin: an exception inside llm.chat must NOT crash the loop."""
    goals.set_goal("cli_rich", "x")

    def _boom(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr("janus.llm.chat", _boom)
    d = goal_loop.after_turn("cli_rich", "out")
    # Falls back to: not achieved, default next prompt, NOT paused
    assert d.achieved is False
    assert d.paused is False
    assert d.next_prompt is not None


# ---------- is_active ----------


def test_is_active_no_goal():
    assert goal_loop.is_active("cli_rich") is False


def test_is_active_active_goal():
    goals.set_goal("cli_rich", "x", turn_budget=10)
    assert goal_loop.is_active("cli_rich") is True


def test_is_active_paused_goal_false():
    goals.set_goal("cli_rich", "x", turn_budget=10)
    goals.pause("cli_rich")
    assert goal_loop.is_active("cli_rich") is False


def test_is_active_exhausted_budget_false():
    g = goals.set_goal("cli_rich", "x", turn_budget=1)
    g.turns_used = 1
    goals.save("cli_rich", g)
    assert goal_loop.is_active("cli_rich") is False


# ---------- plan-mode auto-leave (slash handler) ----------


def test_set_goal_in_plan_mode_auto_leaves_plan():
    """Pin: /goal <text> while mode is 'plan' flips to 'default'."""
    reg = sd.SlashRegistry()
    sd.register_shared_handlers(reg)
    ms = permissions.ModeState(current=permissions.PLAN)
    state = {"mode_state": ms}

    handled, out = reg.dispatch("/goal ship the thing", _ctx(state=state))
    assert handled is True
    assert ms.current == permissions.DEFAULT
    assert "left plan mode" in out.lower()


def test_set_goal_in_default_mode_does_not_change_mode():
    reg = sd.SlashRegistry()
    sd.register_shared_handlers(reg)
    ms = permissions.ModeState(current=permissions.DEFAULT)
    state = {"mode_state": ms}

    handled, out = reg.dispatch("/goal ship the thing", _ctx(state=state))
    assert ms.current == permissions.DEFAULT
    assert "left plan mode" not in out.lower()


def test_set_goal_no_mode_state_in_ctx_does_not_crash():
    """Pin: a surface that doesn't populate mode_state still works."""
    reg = sd.SlashRegistry()
    sd.register_shared_handlers(reg)
    handled, out = reg.dispatch("/goal x", _ctx(state={}))
    assert handled is True
    assert "goal set" in out.lower()


# ---------- recent_response_hashes round-trip ----------


def test_recent_response_hashes_persist_across_load(monkeypatch):
    goals.set_goal("cli_rich", "x")
    _patch_judge(monkeypatch, achieved=False, next_step="n")
    goal_loop.after_turn("cli_rich", "alpha")
    g = goals.load("cli_rich")
    assert len(g.recent_response_hashes) == 1
    goal_loop.after_turn("cli_rich", "beta")
    g = goals.load("cli_rich")
    assert len(g.recent_response_hashes) == 2


def test_old_goal_file_without_hashes_loads_clean(tmp_path, monkeypatch):
    """Pin: a v1.37.0 goal file (no recent_response_hashes field) still
    loads — back-compat for users who set goals on v1.37.0 then upgrade.
    """
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
        # no recent_response_hashes → should default to []
    }))
    g = goals.load("cli_rich")
    assert g is not None
    assert g.recent_response_hashes == []


# ---------- version ----------


def test_version_bumped_to_1_37_1():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 37, 1)


def test_judge_model_in_config():
    """Pin: JUDGE_MODEL is exposed + 'judge' is a valid purpose."""
    from janus import config
    assert hasattr(config, "JUDGE_MODEL")
    # model_for_purpose('judge') falls back to MODEL when unset
    assert config.model_for_purpose("judge") == (
        config.JUDGE_MODEL or config.MODEL
    )
