"""Tests for v1.31.5 — plan-review approver gating fix.

FIELD-VALIDATION FINDING (Sam, 2026-05-08, on his Ubuntu VPS):

Sam ran ``/mode plan`` + a planning prompt on both CLI and Telegram.
The model called ``exit_plan_mode``. He expected to see:
  - CLI: cyan-bordered Plan Review panel with [Y]es/[N]o prompt (v1.27.2)
  - Telegram: 📋 *Plan Review* markdown message + 2-button keyboard (v1.30.0)

He saw neither. The model's plans got auto-rubber-stamped with
PLAN_APPROVED before he had any chance to review.

ROOT CAUSE:
``ExitPlanMode.risk = "read"`` (tools/plan_mode.py:82). The
``permissions.decide(read, mode)`` decision in v1.0's matrix
returns ALLOW for read in EVERY mode (default / acceptEdits / plan
/ bypassPermissions). Pre-v1.31.5 the approvers in cli_rich /
telegram / web all checked ``decide()`` BEFORE the exit_plan_mode
special-case, so the special-case was unreachable code — the
approver returned True silently and the model got PLAN_APPROVED
with zero user review.

The unit tests for v1.27.2 / v1.30.0 passed because they
constructed mode + risk inputs that route through ASK
(e.g., risk="write"), bypassing the bug. The production path
(risk="read" + any mode) was never exercised by the tests.

THE FIX:
``is_plan_action(action_label)`` is checked FIRST in each
approver, BEFORE ``permissions.decide()``. Plan actions always
proceed to the render+ask flow regardless of mode.

These tests pin the production path explicitly: each approver,
called with action_label="exit_plan_mode" + risk="read" + the
mode the user actually has, must NOT short-circuit on ALLOW —
must reach the render path.
"""

from __future__ import annotations

import inspect

from janus import permissions, plan_render
from janus.gateways import web as web_mod


# ============================================================
# Pre-condition checks — confirm we're testing the right thing
# ============================================================


def test_exit_plan_mode_is_risk_read():
    """Pin the underlying condition: ExitPlanMode is risk=read.
    If this changes, the v1.31.5 fix shape may need to be revisited."""
    from janus.tools.plan_mode import ExitPlanMode
    assert ExitPlanMode.risk == "read"


def test_decide_read_allows_in_every_mode():
    """Pin the matrix: read class is ALLOW in every mode.
    This is the condition that creates the bug — the fix must
    NOT change this matrix; instead the approvers gate around it."""
    for mode in ("default", "acceptEdits", "plan", "bypassPermissions"):
        assert permissions.decide("read", mode) == permissions.ALLOW, (
            f"matrix changed: read in {mode} no longer ALLOW"
        )


# ============================================================
# cli_rich approver — production path
# ============================================================


def test_cli_rich_approver_plan_check_runs_before_decide():
    """Source pin: in cli_rich._make_mode_approver, the
    ``exit_plan_mode`` check appears BEFORE ``permissions.decide``.
    Pre-v1.31.5 it was after, making it dead code."""
    from janus import cli_rich
    src = inspect.getsource(cli_rich._make_mode_approver)
    decide_idx = src.find("decision = permissions.decide(")
    plan_idx = src.find("exit_plan_mode")
    assert plan_idx != -1, "exit_plan_mode branch not found"
    assert decide_idx != -1, "permissions.decide call not found"
    assert plan_idx < decide_idx, (
        "v1.31.5 regression: exit_plan_mode check is now AFTER "
        "permissions.decide. ExitPlanMode is risk=read which "
        "auto-ALLOWs in every mode, so post-decide check is "
        "unreachable. Move the check above decide."
    )


def test_cli_rich_approver_has_v1_31_5_marker():
    from janus import cli_rich
    src = inspect.getsource(cli_rich._make_mode_approver)
    assert "v1.31.5" in src


def test_cli_rich_approver_calls_render_rich_panel():
    """The approver still uses the v1.27.2 render helper for the
    actual panel rendering."""
    from janus import cli_rich
    src = inspect.getsource(cli_rich._make_mode_approver)
    assert "render_rich_panel" in src


# ============================================================
# telegram approver — production path
# ============================================================


def test_telegram_approver_plan_check_runs_before_decide():
    """Source pin: telegram approver detects is_plan BEFORE
    calling permissions.decide. Pre-v1.31.5 the order was
    decide → ALLOW → return True, leaving plan render unreachable."""
    from janus.gateways import telegram as tg
    src = inspect.getsource(tg._make_approver)
    is_plan_assign = src.find("is_plan = ")
    decide_idx = src.find("decision = permissions.decide(")
    assert is_plan_assign != -1, "is_plan assignment not found"
    assert decide_idx != -1
    assert is_plan_assign < decide_idx, (
        "v1.31.5 regression: is_plan determined AFTER decide; "
        "plan actions auto-ALLOW silently. Move detection up."
    )


def test_telegram_approver_gates_decide_on_not_is_plan():
    """The mode-based decide block must be gated by
    ``if not is_plan:``. Otherwise plan actions hit the ALLOW
    short-circuit and never reach the keyboard."""
    from janus.gateways import telegram as tg
    src = inspect.getsource(tg._make_approver)
    assert "if not is_plan:" in src


def test_telegram_approver_has_v1_31_5_marker():
    from janus.gateways import telegram as tg
    src = inspect.getsource(tg._make_approver)
    assert "v1.31.5" in src


def test_telegram_approver_uses_is_plan_action_helper():
    """Detection goes through the shared helper so all surfaces
    agree on what counts as a plan action."""
    from janus.gateways import telegram as tg
    src = inspect.getsource(tg._make_approver)
    assert "is_plan_action" in src


# ============================================================
# web approver — production path
# ============================================================


def test_web_approver_plan_check_runs_before_decide():
    src = inspect.getsource(web_mod._make_web_approver)
    is_plan_assign = src.find("is_plan = ")
    decide_idx = src.find("decision = permissions.decide(")
    assert is_plan_assign != -1
    assert decide_idx != -1
    assert is_plan_assign < decide_idx, (
        "v1.31.5 regression: web approver decides mode before "
        "checking is_plan. Plan modal will never render."
    )


def test_web_approver_gates_decide_on_not_is_plan():
    src = inspect.getsource(web_mod._make_web_approver)
    assert "if not is_plan:" in src


def test_web_approver_has_v1_31_5_marker():
    src = inspect.getsource(web_mod._make_web_approver)
    assert "v1.31.5" in src


def test_web_approver_uses_is_plan_action_helper():
    src = inspect.getsource(web_mod._make_web_approver)
    assert "is_plan_action" in src


# ============================================================
# Behavioral — cli_rich approver, production-shape inputs
# ============================================================


class _FakeConsole:
    """Captures console.print calls so tests can inspect output."""
    def __init__(self):
        self.calls: list = []
    def print(self, *args, **kwargs):
        self.calls.append(("print", args, kwargs))


def test_cli_rich_approver_plan_action_in_plan_mode_does_not_auto_allow(
    monkeypatch,
):
    """The bug shape: action_label='exit_plan_mode', risk='read',
    mode='plan' must NOT return True silently. It must enter the
    plan-render path (which prompts the user)."""
    from janus import cli_rich
    mode_state = permissions.ModeState()
    mode_state.set("plan")
    console = _FakeConsole()

    # Stub the prompt — auto-decline so the test doesn't hang.
    # The fact that the prompt FUNCTION runs at all is what we're
    # testing; pre-v1.31.5 the approver auto-returned True without
    # reaching the prompt.
    prompt_called = []

    def fake_prompt(prompt_text, default=""):
        prompt_called.append(prompt_text)
        return "n"

    monkeypatch.setattr(
        "prompt_toolkit.prompt", fake_prompt, raising=False,
    )

    approver = cli_rich._make_mode_approver(console, mode_state)
    result = approver(
        "exit_plan_mode",
        "## Plan\n1. Read foo.py\n2. Edit bar.py",
        risk="read",
    )

    # User declined → False
    assert result is False, (
        "approver auto-allowed plan action despite user-facing "
        "prompt — v1.31.5 fix regressed"
    )
    # AND the prompt actually ran (proving we reached the render path)
    assert prompt_called, (
        "prompt never ran — approver short-circuited on "
        "permissions.decide(read, plan) → ALLOW. The v1.31.5 "
        "gating fix is broken."
    )
    # AND the panel was printed
    assert any(
        "print" == call[0] for call in console.calls
    ), "Plan Review panel was never rendered to console"


def test_cli_rich_approver_plan_action_in_default_mode_also_renders(
    monkeypatch,
):
    """Same fix must apply in default mode — read class also
    auto-ALLOWs there."""
    from janus import cli_rich
    mode_state = permissions.ModeState()
    mode_state.set("default")
    console = _FakeConsole()
    prompt_called = []
    monkeypatch.setattr(
        "prompt_toolkit.prompt",
        lambda *a, **kw: (prompt_called.append(a), "n")[1],
        raising=False,
    )

    approver = cli_rich._make_mode_approver(console, mode_state)
    result = approver("exit_plan_mode", "## P", risk="read")
    assert result is False
    assert prompt_called, "plan render did not fire in default mode"


def test_cli_rich_approver_non_plan_action_unchanged(monkeypatch):
    """Regression guard: non-plan actions still take the standard
    mode-based path. fs_read in plan mode should still ALLOW
    silently (no console output, no prompt)."""
    from janus import cli_rich
    mode_state = permissions.ModeState()
    mode_state.set("plan")
    console = _FakeConsole()

    approver = cli_rich._make_mode_approver(console, mode_state)
    result = approver("fs_read", "/path/to/foo.py", risk="read")
    assert result is True, (
        "v1.31.5 regression: fs_read no longer auto-allows in plan "
        "mode. The fix should only affect exit_plan_mode."
    )
    # No console output for ALLOW path
    assert not console.calls, (
        f"unexpected console output for fs_read ALLOW: {console.calls}"
    )


def test_cli_rich_approver_non_plan_write_in_plan_mode_still_denied(
    monkeypatch,
):
    """Regression guard: write in plan mode still DENY (the whole
    point of plan mode). The fix shouldn't accidentally route
    writes through the plan-render path."""
    from janus import cli_rich
    mode_state = permissions.ModeState()
    mode_state.set("plan")
    console = _FakeConsole()
    approver = cli_rich._make_mode_approver(console, mode_state)
    result = approver("fs_write", "/path/foo.py", risk="write")
    assert result is False, (
        "v1.31.5 regression: write in plan mode no longer DENY"
    )


# ============================================================
# Behavioral — web approver, production-shape inputs
# ============================================================


def test_web_approver_plan_action_routes_through_bridge(monkeypatch):
    """web approver in plan mode + risk=read must call
    web_bridge.request_approval (not auto-allow). Mock the bridge
    to capture the call."""
    captured: list[dict] = []

    def fake_request_approval(*, auth_sid, loop, label, details, risk, plan=None):
        captured.append({
            "auth_sid": auth_sid, "label": label, "details": details,
            "risk": risk, "plan": plan,
        })
        return False  # user declined

    from janus.gateways import web_bridge
    monkeypatch.setattr(
        web_bridge, "request_approval", fake_request_approval,
    )

    approver = web_mod._make_web_approver(
        mode="plan", auth_sid="test-sid", loop="fake-loop",  # type: ignore
    )
    result = approver(
        "exit_plan_mode",
        "## Plan\n1. step\n2. step",
        risk="read",
    )

    assert result is False
    assert len(captured) == 1, (
        f"v1.31.5 fix broken: bridge wasn't called in plan mode "
        f"(approver auto-allowed instead). captured={captured}"
    )
    # AND a plan payload was attached (v1.30.0 contract preserved)
    assert captured[0]["plan"] is not None
    assert captured[0]["plan"]["mode"] == "plan"


def test_web_approver_non_plan_in_plan_mode_returns_false_no_bridge(
    monkeypatch,
):
    """Regression guard: write in plan mode still gets DENY
    without hitting the bridge (mode decides outright, no user
    prompt needed)."""
    captured: list[dict] = []
    from janus.gateways import web_bridge
    monkeypatch.setattr(
        web_bridge, "request_approval",
        lambda **kw: captured.append(kw) or False,
    )
    approver = web_mod._make_web_approver(
        mode="plan", auth_sid="test", loop="fake",
    )
    result = approver("fs_write", "/x", risk="write")
    assert result is False
    assert captured == [], (
        "fs_write in plan mode should be DENY without bridge call; "
        "v1.31.5 fix accidentally routed it through plan-render path"
    )


def test_web_approver_plan_payload_built_from_render_helper():
    """The plan payload uses plan_render.build_web_payload (same
    contract as v1.30.0 — tests/test_v1_30_0_plan_render_parity.py
    pins the keys)."""
    parsed = plan_render.parse_plan("1. step\n2. step")
    payload = plan_render.build_web_payload(parsed, "x", mode="plan")
    # Sanity: payload has the keys the web client expects.
    for key in ("mode", "step_count", "files", "body_md", "metric_line"):
        assert key in payload
