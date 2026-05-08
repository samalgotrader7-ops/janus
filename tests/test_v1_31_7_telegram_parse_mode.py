"""Tests for v1.31.7 — Telegram plan-review parse_mode fix.

FIELD-VALIDATION FINDING (Sam, 2026-05-08, Telegram VPS test):

After v1.31.5 made the plan-review approver branch reachable on
Telegram, Sam ran a fresh planning prompt about cost.py budgets.
The model called ``exit_plan_mode`` correctly. The approver
detected ``is_plan`` and reached the ``app.bot.send_message`` call
with ``parse_mode="Markdown"``. Telegram silently rejected the
send because the plan body contained model-generated markdown
that Telegram's v1 parser couldn't handle:
  * ``**bold**`` (v1 only supports single-asterisk bold)
  * Identifiers like ``JANUS_COST_BUDGET_STRICT`` where adjacent
    underscores trip italic detection mid-token

The send failed on the asyncio loop. The approver's executor
thread had already moved on to wait on the approval future. With
no message delivered, no buttons appeared, no click could resolve
the future — Sam saw 10 minutes of "typing" before he interrupted.

ROOT CAUSE:
``parse_mode="Markdown"`` + unpredictable model output =
unreliable. The model output in the plan body is not under our
control; assuming it'll be valid Telegram-flavored markdown is
naive.

THE FIX:
Two parts:
  1. ``plan_render.render_telegram_text`` returns plain-text (no
     markdown emphasis in the header). Box-drawing separator and
     emoji provide visual structure without relying on
     parse_mode.
  2. ``gateways/telegram.py`` ``_make_approver`` sends plan
     reviews with ``parse_mode=None`` (plain text). Generic ASK
     approvals keep ``parse_mode="Markdown"`` because their body
     is constructed by us and is known safe.
  3. The send is now wrapped in a result-await + retry path: if
     send fails for any reason, we log it (so future bugs aren't
     a black box) and retry plain. Better than silent hang.

DESIGN INVARIANTS PINNED:
  * Plan-review messages on Telegram NEVER use parse_mode.
  * ``render_telegram_text`` output contains no markdown emphasis
    chars in the header (no `*`, no `_`, no `` ` `` for the
    structural elements; emoji + box-drawing only).
  * Generic ASK approvals still use parse_mode="Markdown" (those
    bodies are safe).
  * Send failures are logged + retried (no more silent hang).
"""

from __future__ import annotations

import inspect

from janus import plan_render
from janus.gateways import telegram as tg_mod


# ============================================================
# render_telegram_text — plain-text shape
# ============================================================


def test_no_markdown_emphasis_in_header():
    """The plain-text rewrite drops `*Plan Review*` /
    `_metric_` / `` `mode=...` `` from the header."""
    parsed = plan_render.parse_plan("1. step\n2. step")
    out = plan_render.render_telegram_text(parsed, "1. step", mode="plan")
    # Find the first line — that's the header
    header = out.split("\n", 1)[0]
    # No markdown emphasis chars in the header structure
    assert "*" not in header, f"asterisk in header: {header!r}"
    # Underscores are allowed (e.g. ``mode=plan_default``) but no
    # paired-underscore pattern (which would be markdown italic).
    # We can't easily test "no italic-shaped pattern" so just check
    # the specific markers we used to use are gone.
    assert "_Plan Review_" not in header
    assert "`mode=" not in header


def test_header_uses_unicode_for_structure():
    """The new shape relies on emoji + box-drawing for visual
    weight, not markdown."""
    parsed = plan_render.parse_plan("1. step")
    out = plan_render.render_telegram_text(parsed, "1. step", mode="plan")
    assert "📋" in out  # plan emoji (unicode, parse-mode-agnostic)
    assert "─" in out  # box-drawing horizontal separator
    assert "PLAN REVIEW" in out  # uppercase as visual emphasis


def test_metric_line_in_header():
    """Metrics + mode still appear in the plain-text header."""
    parsed = plan_render.parse_plan(
        "1. read foo.py\n2. edit bar.py\n3. test"
    )
    out = plan_render.render_telegram_text(parsed, "x", mode="plan")
    header = out.split("\n", 1)[0]
    assert "3 steps" in header
    assert "mode=plan" in header


def test_body_unchanged_when_under_cap():
    parsed = plan_render.parse_plan("1. step")
    body = "model markdown with **bold** and `code` and _italic_"
    out = plan_render.render_telegram_text(parsed, body)
    # Body content survives verbatim (we don't try to escape it —
    # parse_mode=None means it shows literally, which is fine).
    assert "**bold**" in out
    assert "`code`" in out
    assert "_italic_" in out


def test_body_truncation_message_is_plain():
    """Truncation note now plain text (was italic markdown)."""
    big = "step\n" * 5000
    parsed = plan_render.parse_plan("- a")
    out = plan_render.render_telegram_text(parsed, big, mode="plan")
    assert "truncated" in out.lower()
    # No markdown italic on the truncation note
    assert "_(plan body truncated" not in out


# ============================================================
# Telegram approver — parse_mode source pin
# ============================================================


def test_approver_sets_parse_mode_none_for_plan():
    """Source pin: the plan-review send call uses
    ``parse_mode=None`` (or equivalent). Pre-v1.31.7 it used
    ``parse_mode="Markdown"`` which caused the silent-hang bug."""
    src = inspect.getsource(tg_mod._make_approver)
    # The conditional parse_mode based on is_plan
    assert "plan_parse_mode" in src or 'parse_mode = None' in src
    # No unconditional parse_mode="Markdown" on the keyboard send
    # (the OLD shape was a single Markdown send for both branches)
    # We expect EITHER a parse_mode=None branch OR an is_plan ternary.
    # Source-pin: the v1.31.7 marker is present
    assert "v1.31.7" in src


def test_approver_logs_send_failure():
    """Source pin: send failure is logged, not swallowed.
    Pre-v1.31.7 the send result was never awaited; failures
    on the asyncio loop never reached the executor thread."""
    src = inspect.getsource(tg_mod._make_approver)
    # logging import / call
    assert "logging" in src
    # send_fut.result(timeout=...) — wait for send to complete
    assert "send_fut.result(" in src or "send_fut" in src


def test_approver_falls_back_to_plain_on_send_failure():
    """If the first send fails (parse error), retry with plain
    text. Defense in depth — even if parse_mode logic regresses,
    the fallback ensures the user sees buttons."""
    src = inspect.getsource(tg_mod._make_approver)
    # retry path
    assert "fallback_coro" in src or "retry" in src.lower()


def test_approver_send_has_timeout():
    """The result wait must have a finite timeout. Without one,
    a network-level hang in send_message could block the
    executor thread indefinitely."""
    src = inspect.getsource(tg_mod._make_approver)
    # at least one timeout=N argument near the send
    region_start = src.find("send_fut")
    if region_start == -1:
        # Fallback search — the v1.31.7 fix should have added send_fut
        # but let the test surface a clear message if it's renamed.
        raise AssertionError(
            "send_fut variable not found in approver source — "
            "v1.31.7 fix shape changed; update test"
        )
    region = src[region_start:region_start + 800]
    assert "timeout=" in region


# ============================================================
# Generic ASK approvals UNCHANGED (still Markdown)
# ============================================================


def test_generic_ask_still_uses_markdown():
    """Regression guard: non-plan ASK approvals still use
    parse_mode="Markdown". The fix only applies to plan
    reviews."""
    src = inspect.getsource(tg_mod._make_approver)
    # The conditional should preserve "Markdown" for non-plan
    assert '"Markdown"' in src or "'Markdown'" in src


# ============================================================
# Module surface
# ============================================================


def test_render_telegram_text_in_all():
    assert "render_telegram_text" in plan_render.__all__


def test_telegram_body_cap_unchanged():
    """The 3600-char cap still applies — no regression on the
    Telegram message-length safety."""
    assert plan_render.TELEGRAM_BODY_CAP == 3600
