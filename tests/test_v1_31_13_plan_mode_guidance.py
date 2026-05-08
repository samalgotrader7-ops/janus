"""Tests for v1.31.13 — ExitPlanMode result-message guidance.

FIELD-VALIDATION FINDING (Sam, 2026-05-08, after the v1.31.10
text-reply fallback worked):

After Sam typed N to refine the plan, exit_plan_mode returned
the bare sentinel "PLAN_REFUSED". The model received that
single token with no context and produced: "Got it — you were
testing the plan flow. The plan is solid and ready to execute
whenever you say go."

Problem: "ready to execute" is wrong. PLAN_REFUSED does NOT
trigger the post-turn mode-switch (only PLAN_APPROVED does).
Mode is still 'plan'. Any subsequent fs_write / fs_edit /
shell call would be blocked by mode 'plan'. The model didn't
know because the bare sentinel carried no guidance.

THE FIX:
Both PLAN_APPROVED and PLAN_REFUSED now return enriched
guidance messages instead of bare sentinels. The model gets
explicit instructions about what mode is now active and what
tools can/cannot be called next.

Sentinel SUBSTRINGS are preserved at the start of each message
("PLAN_APPROVED — ..." and "PLAN_REFUSED — ...") so existing
detectors that use ``PLAN_APPROVED in result_preview`` (cli_rich
post-turn mode-switch) keep working unchanged.

DESIGN INVARIANTS PINNED:
  * Tool returns the full guidance message, not the bare sentinel.
  * Sentinel substring still appears at the start of each message
    (preserves backward compat for cli_rich detector).
  * PLAN_REFUSED message tells the model: mode is still plan,
    DON'T attempt write/exec, ask user to refine via clarify.
  * PLAN_APPROVED message tells the model: mode-switch is post-
    turn, in-turn writes may still block, finish the response.
"""

from __future__ import annotations

from janus.tools.plan_mode import (
    ExitPlanMode,
    PLAN_APPROVED,
    PLAN_REFUSED,
    PLAN_APPROVED_MESSAGE,
    PLAN_REFUSED_MESSAGE,
)


def _approve(*a, **kw): return True
def _deny(*a, **kw): return False


# ============================================================
# Sentinel preservation (cli_rich detector still works)
# ============================================================


def test_plan_approved_message_contains_sentinel():
    """cli_rich uses ``PLAN_APPROVED in str(result_preview)``.
    The new message must keep that substring."""
    assert PLAN_APPROVED in PLAN_APPROVED_MESSAGE


def test_plan_refused_message_contains_sentinel():
    """Symmetry — preserve the refused sentinel as a substring too."""
    assert PLAN_REFUSED in PLAN_REFUSED_MESSAGE


def test_sentinels_are_at_message_start():
    """Position the sentinel at the start so it's visually
    obvious in the trace + result_preview snippet."""
    assert PLAN_APPROVED_MESSAGE.startswith(PLAN_APPROVED)
    assert PLAN_REFUSED_MESSAGE.startswith(PLAN_REFUSED)


# ============================================================
# Tool returns the enriched message
# ============================================================


def test_tool_run_returns_full_message_on_approve():
    out = ExitPlanMode().run(
        {"plan": "1. read foo.py\n2. edit bar.py"}, _approve,
    )
    assert out == PLAN_APPROVED_MESSAGE
    # Smoke-test: it's not just the bare sentinel
    assert len(out) > len(PLAN_APPROVED) + 20


def test_tool_run_returns_full_message_on_refuse():
    out = ExitPlanMode().run(
        {"plan": "1. delete everything"}, _deny,
    )
    assert out == PLAN_REFUSED_MESSAGE
    assert len(out) > len(PLAN_REFUSED) + 20


# ============================================================
# Refused message content — model guidance
# ============================================================


def test_refused_message_says_mode_still_plan():
    """The model must learn that mode hasn't switched."""
    msg = PLAN_REFUSED_MESSAGE.lower()
    assert "still" in msg
    assert "plan" in msg


def test_refused_message_warns_about_blocked_tools():
    """The model must know write/exec calls won't work."""
    msg = PLAN_REFUSED_MESSAGE.lower()
    assert "blocked" in msg
    # Specific tool names so the model recognizes the names it'd
    # otherwise call by reflex.
    assert "fs_write" in msg or "fs_edit" in msg
    assert "shell" in msg


def test_refused_message_suggests_iterate_via_clarify():
    """The model should be told: don't execute; ask + iterate."""
    msg = PLAN_REFUSED_MESSAGE.lower()
    # Either "clarify" or "ask the user" — directs the model to
    # the right next step.
    assert "clarify" in msg or "ask" in msg


def test_refused_message_mentions_mode_switch_command():
    """User-facing instruction so the model can guide the user
    on how to actually start executing."""
    msg = PLAN_REFUSED_MESSAGE.lower()
    assert "mode default" in msg or "mode auto" in msg or "/mode" in msg


# ============================================================
# Approved message content — model guidance
# ============================================================


def test_approved_message_explains_mode_switch_is_post_turn():
    """The model must understand it can plan ahead but in-turn
    writes might still hit plan-mode blocks before the post-turn
    watcher fires."""
    msg = PLAN_APPROVED_MESSAGE.lower()
    assert "post-turn" in msg or "end of this turn" in msg


def test_approved_message_says_default_mode_coming():
    """Tell the model the framework will switch mode to default."""
    msg = PLAN_APPROVED_MESSAGE.lower()
    assert "default" in msg


# ============================================================
# Backward compat — existing detector logic
# ============================================================


def test_cli_rich_detector_still_matches():
    """Smoke test the substring check that cli_rich does:
    ``PLAN_APPROVED in str(entry.get("result_preview"))``"""
    out = ExitPlanMode().run({"plan": "x"}, _approve)
    # Mimic the cli_rich detector
    detected = PLAN_APPROVED in str(out)
    assert detected is True


def test_cli_rich_detector_does_not_match_refused():
    """The detector should NOT fire on refused — that would
    incorrectly switch mode out of plan."""
    out = ExitPlanMode().run({"plan": "x"}, _deny)
    detected = PLAN_APPROVED in str(out)
    assert detected is False


# ============================================================
# Source pin
# ============================================================


def test_v1_31_13_marker_in_module():
    import inspect
    from janus.tools import plan_mode
    src = inspect.getsource(plan_mode)
    assert "v1.31.13" in src


def test_module_exports_messages():
    """The two message constants are importable for tests +
    future surfaces that want to reference them."""
    from janus.tools import plan_mode
    assert hasattr(plan_mode, "PLAN_APPROVED_MESSAGE")
    assert hasattr(plan_mode, "PLAN_REFUSED_MESSAGE")
