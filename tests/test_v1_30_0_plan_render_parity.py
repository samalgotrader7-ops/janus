"""Tests for v1.30.0 — per-surface plan-review rendering parity.

v1.27.2 wired structured Plan Review rendering into cli_rich.
v1.30.0 extends the same shape to telegram (Markdown body + 2-button
keyboard) and web (dedicated modal with metric pills + step list +
file chips). All three surfaces share ``plan_render.parse_plan`` and
the new helper functions:

  * ``plan_render.render_telegram_text(parsed, plan_text, mode)`` —
    Markdown body for Telegram messages.
  * ``plan_render.build_web_payload(parsed, plan_text, mode)`` —
    dict the SSE ``approval_pending`` event includes when the action
    is ExitPlanMode; web client renders the plan modal off it.

Cross-surface invariants enforced here:
  * Plan-mode approvals NEVER offer session/always grants. Every plan
    deserves a fresh decision (matches v1.27.2 cli_rich narrowing).
  * The shared parser drives both surfaces — no surface re-parses the
    plan text differently.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from janus import plan_render
from janus.gateways import telegram as tg_mod
from janus.gateways import web as web_mod
from janus.gateways import web_bridge


_MARKER = "v1.30.0"


# ============================================================
# plan_render — new cross-surface helpers
# ============================================================


def test_render_telegram_text_has_metric_header():
    parsed = plan_render.parse_plan(
        "1. read foo.py\n2. edit bar.py\n3. run tests\n"
    )
    out = plan_render.render_telegram_text(parsed, "1. step\n2. step", mode="plan")
    assert "Plan Review" in out
    assert "3 steps" in out  # metric line counted
    assert "mode=plan" in out


def test_render_telegram_text_caps_long_body():
    big = "step\n" * 5000  # ~25k chars
    parsed = plan_render.parse_plan("- a\n- b")
    out = plan_render.render_telegram_text(parsed, big, mode="plan")
    # Telegram message limit is 4096; we cap body at 3600 + headroom
    assert len(out) < 4096
    assert "truncated" in out.lower()


def test_render_telegram_text_short_body_not_truncated():
    parsed = plan_render.parse_plan("1. x")
    out = plan_render.render_telegram_text(parsed, "1. x", mode="plan")
    assert "truncated" not in out.lower()


def test_render_telegram_text_uses_markdown_emphasis():
    """Body uses *bold* / _italic_ markdown so Telegram renders it."""
    parsed = plan_render.parse_plan("1. x")
    out = plan_render.render_telegram_text(parsed, "1. x")
    assert "*Plan Review*" in out
    # Mode tag uses backticks (monospace)
    assert "`mode=" in out


def test_render_telegram_text_includes_emoji_marker():
    """Visual cue distinguishes plan review from generic approval."""
    parsed = plan_render.parse_plan("1. x")
    out = plan_render.render_telegram_text(parsed, "1. x")
    assert "📋" in out


def test_build_web_payload_shape():
    parsed = plan_render.parse_plan(
        "1. read `foo.py`\n2. edit `bar.py:42`\n3. run tests"
    )
    payload = plan_render.build_web_payload(parsed, "1. step", mode="plan")
    assert payload["mode"] == "plan"
    assert payload["step_count"] == 3
    assert payload["file_count"] >= 2
    assert "foo.py" in payload["files"]
    assert "metric_line" in payload
    assert payload["body_md"] == "1. step"
    assert payload["files_truncated"] is False
    assert isinstance(payload["steps"], list)
    assert payload["steps"] == ["read `foo.py`", "edit `bar.py:42`", "run tests"]


def test_build_web_payload_caps_files_at_16():
    plan = "files:\n" + "\n".join(f"- `f{i}.py`" for i in range(40))
    parsed = plan_render.parse_plan(plan)
    payload = plan_render.build_web_payload(parsed, plan, mode="plan")
    assert len(payload["files"]) == 16
    assert payload["files_truncated"] is True
    assert payload["file_count"] >= 40  # full count preserved


def test_build_web_payload_is_json_serializable():
    """SSE wire format requires JSON. Make sure no dataclass leaks."""
    import json as _json
    parsed = plan_render.parse_plan("1. step")
    payload = plan_render.build_web_payload(parsed, "1. step")
    # Must round-trip without error
    _json.loads(_json.dumps(payload))


def test_build_web_payload_estimated_tool_calls_optional():
    """When the model didn't estimate, the field stays None (not 0)."""
    parsed = plan_render.parse_plan("1. step\n2. step")
    payload = plan_render.build_web_payload(parsed, "1. step\n2. step")
    assert payload["estimated_tool_calls"] is None


def test_build_web_payload_propagates_estimated_tool_calls():
    parsed = plan_render.parse_plan(
        "1. step\n2. step\n\nEstimated 7 tool calls."
    )
    payload = plan_render.build_web_payload(parsed, "x")
    assert payload["estimated_tool_calls"] == 7


def test_module_exports_new_helpers():
    """__all__ pin: surfaces import via the module surface, not internals."""
    assert "render_telegram_text" in plan_render.__all__
    assert "build_web_payload" in plan_render.__all__
    assert "TELEGRAM_BODY_CAP" in plan_render.__all__


# ============================================================
# Telegram gateway — source pins
# ============================================================


def test_telegram_imports_plan_render_in_approver():
    """Source-pin: the approver function itself imports plan_render
    for the plan-action branch."""
    src = inspect.getsource(tg_mod._make_approver)
    assert "plan_render" in src
    assert _MARKER in src


def test_telegram_branches_on_is_plan_action():
    """Detection happens via the shared helper, not a string match."""
    src = inspect.getsource(tg_mod._make_approver)
    assert "is_plan_action" in src


def test_telegram_uses_render_telegram_text():
    """Body is built via the shared render helper, not ad-hoc string
    formatting that could drift from cli_rich."""
    src = inspect.getsource(tg_mod._make_approver)
    assert "render_telegram_text" in src


def test_telegram_plan_keyboard_is_two_buttons():
    """v1.27.2 cli_rich narrowing: plan-mode prompt is intentionally
    [Y]es / [N]o with NO session/always grants. Telegram parity must
    drop the 4-button keyboard for plan reviews."""
    src = inspect.getsource(tg_mod._make_approver)
    # Find the plan-mode keyboard region.
    region_start = src.find(_MARKER)
    assert region_start != -1
    # Take the slice up to the next "else:" (where the legacy
    # 4-button keyboard lives).
    region_end = src.find("else:", region_start)
    region = src[region_start:region_end if region_end != -1 else region_start + 2000]
    # Plan keyboard must NOT include sess/always callback data.
    assert "appr:{token}:sess" not in region
    assert "appr:{token}:always" not in region
    # But MUST include once + deny.
    assert ":once" in region
    assert ":deny" in region


def test_telegram_plan_branch_falls_back_safely():
    """If parsing/rendering hiccups, fall through to a generic body —
    the chat must NEVER lose an approval prompt to a render bug."""
    src = inspect.getsource(tg_mod._make_approver)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 4000]
    assert "try:" in region
    assert "except Exception:" in region


# ============================================================
# Web gateway — bridge + approver
# ============================================================


def test_web_bridge_request_approval_accepts_plan():
    sig = inspect.signature(web_bridge.request_approval)
    assert "plan" in sig.parameters
    p = sig.parameters["plan"]
    # Optional, defaults to None
    assert p.default is None


def test_web_bridge_event_includes_plan_payload(monkeypatch):
    """Behavioral pin: when ``plan`` is passed, the broadcast event
    carries it. We capture the broadcast call without running an
    asyncio loop."""
    web_bridge._reset_for_tests()

    captured: list[dict] = []

    def fake_broadcast(loop, auth_sid, event):  # noqa: ARG001
        captured.append(event)

    monkeypatch.setattr(web_bridge, "_broadcast_from_thread", fake_broadcast)
    # Don't actually block — we only care about the event shape.
    monkeypatch.setattr(
        web_bridge,
        "_approval_timeout",
        lambda: 0.01,
    )

    plan_payload = {"mode": "plan", "step_count": 3, "files": ["foo.py"]}
    web_bridge.request_approval(
        auth_sid="sid-test",
        loop=None,  # type: ignore[arg-type]
        label="exit_plan_mode",
        details="1. step",
        risk="read",
        plan=plan_payload,
    )
    # First broadcast is the approval_pending event.
    pending = next(e for e in captured if e["type"] == "approval_pending")
    assert pending["plan"] == plan_payload
    web_bridge._reset_for_tests()


def test_web_bridge_event_omits_plan_when_none(monkeypatch):
    """Generic approvals (no plan) must NOT include a `plan` key —
    the client distinguishes by presence."""
    web_bridge._reset_for_tests()
    captured: list[dict] = []
    monkeypatch.setattr(
        web_bridge, "_broadcast_from_thread",
        lambda loop, sid, ev: captured.append(ev),
    )
    monkeypatch.setattr(web_bridge, "_approval_timeout", lambda: 0.01)
    web_bridge.request_approval(
        auth_sid="sid-test", loop=None,  # type: ignore[arg-type]
        label="fs_write", details="...", risk="write",
    )
    pending = next(e for e in captured if e["type"] == "approval_pending")
    assert "plan" not in pending
    web_bridge._reset_for_tests()


def test_web_bridge_list_pending_includes_plan(monkeypatch):
    """Reconnect-after-page-reload must hydrate the plan modal, not
    fall back to the generic approval modal."""
    web_bridge._reset_for_tests()
    monkeypatch.setattr(
        web_bridge, "_broadcast_from_thread", lambda *a, **k: None
    )
    monkeypatch.setattr(web_bridge, "_approval_timeout", lambda: 0.05)
    plan_payload = {"mode": "plan", "step_count": 2}
    # Run the request in a thread so we can check pending state mid-flight.
    import threading
    t = threading.Thread(
        target=web_bridge.request_approval,
        kwargs=dict(
            auth_sid="sid-test", loop=None,
            label="exit_plan_mode", details="1. step", risk="read",
            plan=plan_payload,
        ),
        daemon=True,
    )
    t.start()
    # Spin briefly until the approval is registered.
    import time
    deadline = time.time() + 1.0
    pending: list[dict] = []
    while time.time() < deadline:
        pending = web_bridge.list_pending_approvals("sid-test")
        if pending:
            break
        time.sleep(0.005)
    assert pending, "approval was not registered before the test poll"
    assert pending[0].get("plan") == plan_payload
    t.join(timeout=1.0)
    web_bridge._reset_for_tests()


def test_web_approver_builds_plan_payload_for_exit_plan_mode():
    """Source-pin: _make_web_approver detects exit_plan_mode and
    constructs a plan payload via plan_render.build_web_payload."""
    src = inspect.getsource(web_mod._make_web_approver)
    assert _MARKER in src
    assert "plan_render" in src
    assert "is_plan_action" in src
    assert "build_web_payload" in src
    # The kwarg goes into request_approval as `plan=`.
    assert "plan=plan_payload" in src or "plan=" in src


def test_web_approver_plan_failure_falls_back():
    """Render failure must NOT block the approval flow — fall back to
    generic modal."""
    src = inspect.getsource(web_mod._make_web_approver)
    assert "try:" in src
    assert "except Exception:" in src


# ============================================================
# Web frontend — static asset pins
# ============================================================


_STATIC = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"


def test_index_html_has_plan_modal():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="modal-plan"' in html
    assert 'id="plan-metrics"' in html
    assert 'id="plan-files"' in html
    assert 'id="plan-steps"' in html
    assert 'id="plan-body"' in html
    assert 'id="plan-approve"' in html
    assert 'id="plan-deny"' in html


def test_app_js_has_show_plan_modal():
    js = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert "showPlanModal" in js
    # Routing — plan payload presence triggers plan modal not generic.
    assert "data.plan" in js
    # State machine carries the new kind.
    assert "'plan'" in js
    # submitApproval must accept plan kind too — same /api/approve POST.
    assert "activeKind !== 'plan'" in js or "activeKind === 'plan'" in js


def test_app_css_has_plan_chips_and_steps():
    css = (_STATIC / "app.css").read_text(encoding="utf-8")
    assert ".modal-plan-files" in css
    assert ".modal-plan-steps" in css
    assert ".file-chip" in css


def test_setup_modals_wires_plan_buttons():
    js = (_STATIC / "app.js").read_text(encoding="utf-8")
    # Both buttons are wired in setupModals.
    assert "plan-approve" in js
    assert "plan-deny" in js
