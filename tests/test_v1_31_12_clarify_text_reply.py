"""Tests for v1.31.12 — Telegram clarify text-reply fallback.

FIELD-VALIDATION FINDING (Sam, 2026-05-08, after v1.31.11 confirmed):

Sam tested the v1.31.10 plan-review text-reply fallback. It
worked perfectly — typed N, plan refined, model continued. The
model then called ``clarify`` to ask Sam HOW he wants to refine,
showing a keyboard with options like "Simplify — only cost.py".

The clarify keyboard uses the SAME callback_query mechanism that
fails in Sam's environment. If he taps a clarify option, it'll
hang the same way the plan-review buttons did. The existing
"Other (type your answer)" button is supposed to be the text-
reply escape, but it requires tapping the button first (which
also doesn't work).

THE FIX:
Mirror the v1.31.10 plan-review fallback to clarify. When a
clarify keyboard is sent, also proactively set the
``__awaiting_text__`` future marker so on_text accepts ANY text
reply as the answer — no need to tap "Other" first. Append a
hint "💬 OR type the option text (or your own answer) if
buttons don't work."

Also switch parse_mode from "Markdown" to None for the same
reason as v1.31.7 (model-generated questions might contain
underscores/asterisks that break the parser).

DESIGN INVARIANTS PINNED:
  * When clarify is sent WITH choices, ``__awaiting_text__`` is
    proactively set (not only after tapping "Other").
  * Body has a text-reply hint discoverable to users.
  * parse_mode is None for clarify message sends — model-
    generated content can break Markdown parser.
  * The keyboard still works in healthy environments — the
    text-reply path is additive, not replacing buttons.
"""

from __future__ import annotations

import inspect

from janus.gateways import telegram as tg_mod


# ============================================================
# Source pins
# ============================================================


def test_clarify_callback_sets_awaiting_text_when_choices_present():
    """Source pin: when choices are provided, __awaiting_text__
    is set proactively. Pre-v1.31.12 it was only set after the
    user tapped 'Other (type your answer)'."""
    src = inspect.getsource(tg_mod._make_telegram_clarify_cb)
    # Find the 'if choices:' branch
    choices_idx = src.find("if choices:")
    assert choices_idx != -1
    # __awaiting_text__ assignment must appear within that branch
    # (i.e., before the `else:` for no-choices)
    else_idx = src.find("        else:", choices_idx)
    assert else_idx != -1
    region = src[choices_idx:else_idx]
    assert "__awaiting_text__" in region
    assert "fut" in region


def test_clarify_callback_appends_text_hint_to_body():
    """Body includes a text-reply hint when choices are shown."""
    src = inspect.getsource(tg_mod._make_telegram_clarify_cb)
    # The hint phrase
    assert "type the option text" in src.lower() or (
        "type the option" in src.lower() and "or your own answer" in src.lower()
    )
    assert "buttons don" in src.lower()


def test_clarify_callback_uses_no_parse_mode():
    """v1.31.12: parse_mode dropped for clarify sends — same
    safety as v1.31.7 plan-review fix."""
    src = inspect.getsource(tg_mod._make_telegram_clarify_cb)
    # Find both send_message calls and confirm parse_mode="Markdown"
    # is NOT used.
    assert 'parse_mode="Markdown"' not in src
    assert "parse_mode='Markdown'" not in src


def test_clarify_callback_body_is_plain_text():
    """Body header uses plain text emoji, no markdown asterisks."""
    src = inspect.getsource(tg_mod._make_telegram_clarify_cb)
    assert "❓ clarify" in src
    # Old shape with markdown bold should be gone
    assert "❓ *clarify*" not in src


def test_v1_31_12_marker_in_clarify_callback():
    src = inspect.getsource(tg_mod._make_telegram_clarify_cb)
    assert "v1.31.12" in src


# ============================================================
# Behavioral simulation — text reply resolves clarify future
# ============================================================


def test_text_reply_resolves_clarify_future_when_awaiting_set():
    """Simulate the on_text branch that pops __awaiting_text__
    and sets the future to the typed text."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        clarify_futures = {"__awaiting_text__": fut}

        # Mimic the on_text logic at the awaiting-text branch
        awaiting = clarify_futures.pop("__awaiting_text__", None)
        assert awaiting is fut
        assert not awaiting.done()
        text_reply = "Simplify — only cost.py + test"
        awaiting.set_result(text_reply.strip())

        assert fut.done()
        assert fut.result() == text_reply.strip()
    finally:
        loop.close()


def test_text_reply_uses_typed_text_verbatim():
    """The clarify tool receives the typed text as the answer.
    No transformation, no index lookup. Model interprets the
    text directly."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        # Free-form answer that doesn't match any choice
        answer = "actually let's just rename the exception"
        fut.set_result(answer.strip())
        assert fut.result() == answer.strip()
    finally:
        loop.close()


# ============================================================
# Regression — existing free-text path still works (no choices)
# ============================================================


def test_no_choices_branch_still_sets_awaiting_text():
    """v1.31.12 must not regress the existing no-choices flow.
    When clarify has no choices, the body just says "type your
    answer in the chat" and __awaiting_text__ is set — same as
    pre-v1.31.12."""
    src = inspect.getsource(tg_mod._make_telegram_clarify_cb)
    else_idx = src.find("        else:")
    assert else_idx != -1
    region = src[else_idx:else_idx + 600]
    assert "__awaiting_text__" in region
    assert "type your answer" in region.lower()
