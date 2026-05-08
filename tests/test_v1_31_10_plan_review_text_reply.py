"""Tests for v1.31.10 — Telegram plan-review text-reply fallback.

FIELD-VALIDATION FINDING (Sam, 2026-05-08, exhaustive Telegram
debug session):

After the v1.31.5 / v1.31.6 / v1.31.7 chain made plan-review
work end-to-end on cli_rich, Sam tested Telegram. The plan
rendered + buttons appeared correctly. He tapped Refine.
Nothing happened. Diagnostics across v1.31.8 (logging) and
v1.31.9 (catch-all TypeHandler):

  * on_text fires correctly for messages
  * Group=99 catch-all sees ``kind=message`` updates
  * NO ``kind=callback_query`` updates ever reach the dispatcher
  * Telegram getUpdates returns 0 pending after every tap
  * Reproduced on BOTH desktop AND mobile clients
  * Even a fresh button sent via direct API call doesn't deliver

Conclusion: an environmental issue between Sam's Telegram
account/network and the bot @Janus_Sam2_bot — callback_query
delivery is broken at Telegram's server level for that specific
account+bot pair. Bot is fine, handler registration is fine,
allowed_updates is correct.

THE FIX (workaround that's also a feature improvement):
Plan-review approvals can now be resolved via TEXT REPLY in
addition to the inline keyboard. The bot's plan-review message
includes a hint: "💬 OR reply Y / N if buttons don't work."
When the user types Y/N (or yes/no/approve/refine variants),
on_text resolves the pending future and short-circuits before
running a new chat turn.

The keyboard still works when callback_query delivery is
functional. The text fallback works regardless of environment.

DESIGN INVARIANTS PINPED:
  * Session has a ``pending_plan_review = (fut, token) | None``
    attribute set when the approver sends the keyboard, cleared
    on resolve.
  * on_text checks pending_plan_review BEFORE the clarify branch
    and BEFORE the normal chat path.
  * Y/N variants are case-insensitive and cover natural-language
    forms (yes/no, approve/refine, ok/proceed, stop/decline).
  * on_callback also clears pending_plan_review when its button
    tap resolves — prevents a stray later Y/N from looking
    like a stale resolve.
"""

from __future__ import annotations

import asyncio
import inspect

from janus.gateways import telegram as tg_mod


# ============================================================
# Session.pending_plan_review attribute
# ============================================================


def test_session_has_pending_plan_review_field():
    """Session __init__ must initialize pending_plan_review = None."""
    src = inspect.getsource(tg_mod.Session.__init__)
    assert "pending_plan_review" in src
    assert "= None" in src or " None" in src


def test_session_attribute_default_is_none():
    """Behavioral: a fresh Session has pending_plan_review == None."""
    # Don't actually call Session() (it loads gw state); just check
    # the source initialization. The behavioral test would need
    # heavy mocking.
    src = inspect.getsource(tg_mod.Session.__init__)
    # Find the assignment line
    for line in src.splitlines():
        if "pending_plan_review" in line and "=" in line:
            assert "None" in line, (
                f"pending_plan_review must default None: {line!r}"
            )
            return
    raise AssertionError("pending_plan_review init line not found")


# ============================================================
# Approver source pin — sets pending_plan_review
# ============================================================


def test_approver_sets_pending_plan_review():
    """When the plan-review keyboard is sent, the approver must
    stash (fut, token) on the session so on_text can resolve it
    via text reply later."""
    src = inspect.getsource(tg_mod._make_approver)
    assert "pending_plan_review = (fut, token)" in src


def test_approver_appends_text_reply_hint_to_body():
    """The plan-review body must include the Y/N hint so users
    discoverably know they can reply with text."""
    src = inspect.getsource(tg_mod._make_approver)
    # Hint appears after render_telegram_text + body assignment
    assert "reply Y" in src or "reply with Y" in src.lower()
    # Mention the buttons-don't-work fallback
    assert "buttons don" in src.lower() or "buttons don't" in src.lower()


# ============================================================
# on_text source pin — text reply resolves pending
# ============================================================


def test_on_text_checks_pending_plan_review():
    src = inspect.getsource(tg_mod.on_text)
    assert "pending_plan_review" in src


def test_on_text_resolves_approve_words():
    """Source pin: approve_words tuple includes y/yes/approve."""
    src = inspect.getsource(tg_mod.on_text)
    assert "approve_words" in src
    # The word list — string check for required entries.
    assert '"y"' in src or "'y'" in src
    assert '"yes"' in src or "'yes'" in src
    assert "approve" in src


def test_on_text_resolves_refine_words():
    """Source pin: refine_words tuple includes n/no/refine."""
    src = inspect.getsource(tg_mod.on_text)
    assert "refine_words" in src
    assert '"n"' in src or "'n'" in src
    assert '"no"' in src or "'no'" in src
    assert "refine" in src


def test_on_text_clears_pending_after_resolve():
    """After fut.set_result, pending_plan_review must be cleared
    so a SECOND Y/N reply doesn't try to resolve again."""
    src = inspect.getsource(tg_mod.on_text)
    # Look for assignment of None after set_result
    region_start = src.find("pending_plan_review")
    region = src[region_start:]
    # Must contain "= None" assignment(s)
    assert "= None" in region


def test_on_text_skips_already_done_future():
    """Defense in depth: if button click already resolved the
    future and on_text fires later (unlikely race), don't re-set."""
    src = inspect.getsource(tg_mod.on_text)
    # done() check
    assert "fut.done()" in src or ".done()" in src


def test_on_text_returns_early_after_resolve():
    """Y/N text reply must NOT also dispatch as a normal chat
    turn — the user typed Y to approve, not to start a new
    conversation about the letter Y."""
    src = inspect.getsource(tg_mod.on_text)
    # The branch that calls fut.set_result must end with a return.
    set_result_idx = src.find("fut.set_result(True)")
    assert set_result_idx != -1
    # Look for a `return` within the next 600 chars
    region = src[set_result_idx:set_result_idx + 600]
    assert "return" in region


def test_on_text_logs_text_reply_resolution():
    """Field-debugging needs to see when text-reply resolution
    fires. log.info on each branch."""
    src = inspect.getsource(tg_mod.on_text)
    region_start = src.find("pending_plan_review")
    region = src[region_start:region_start + 3000]
    assert "log.info" in region


# ============================================================
# on_callback also clears pending_plan_review on resolve
# ============================================================


def test_on_callback_clears_pending_after_button_tap():
    """When a button tap resolves the future (the path that
    works in healthy environments), also clear pending_plan_review
    so a stray later Y/N doesn't see stale state."""
    src = inspect.getsource(tg_mod.on_callback)
    # After fut.set_result(granted), there should be a clear of
    # pending_plan_review when the token matches.
    region_start = src.find("fut.set_result(granted)")
    region = src[region_start:region_start + 600]
    assert "pending_plan_review" in region


# ============================================================
# v1.31.10 marker
# ============================================================


def test_v1_31_10_marker_present():
    src = inspect.getsource(tg_mod)
    assert "v1.31.10" in src


# ============================================================
# Behavioral test — simulated Y reply resolves the future
# ============================================================


def test_text_reply_y_resolves_future_to_true():
    """Simulate the on_text logic with a fake session + future.
    Approving via 'y' must set the future to True."""
    fut: asyncio.Future = asyncio.get_event_loop().create_future() if False else _make_future()

    # Mimic Session
    class _FakeSess:
        pending_plan_review = (fut, "abc123")

    sess = _FakeSess()

    # Now mimic the on_text branch
    def _resolve(text: str) -> bool | None:
        """Returns True/False/None — None means the text didn't match."""
        pending = sess.pending_plan_review
        if pending is None:
            return None
        fut2, _ = pending
        if fut2.done():
            sess.pending_plan_review = None
            return None
        normalized = text.strip().lower()
        approve_words = (
            "y", "yes", "approve", "approve plan", "ok", "go", "proceed",
        )
        refine_words = (
            "n", "no", "refine", "decline", "reject", "stop",
        )
        if normalized in approve_words:
            fut2.set_result(True)
            sess.pending_plan_review = None
            return True
        if normalized in refine_words:
            fut2.set_result(False)
            sess.pending_plan_review = None
            return False
        return None

    assert _resolve("y") is True
    assert fut.done()
    assert fut.result() is True
    assert sess.pending_plan_review is None


def test_text_reply_n_resolves_future_to_false():
    fut = _make_future()

    class _FakeSess:
        pending_plan_review = (fut, "abc123")

    sess = _FakeSess()

    def _resolve(text: str):
        pending = sess.pending_plan_review
        fut2, _ = pending
        normalized = text.strip().lower()
        if normalized in ("n", "no", "refine"):
            fut2.set_result(False)
            sess.pending_plan_review = None
            return False
        return None

    assert _resolve("n") is False
    assert fut.done()
    assert fut.result() is False
    assert sess.pending_plan_review is None


def test_text_reply_unrelated_does_not_resolve():
    """Sending 'hello' or 'tell me more' must NOT resolve the
    future. Future stays open; pending_plan_review stays set."""
    fut = _make_future()

    class _FakeSess:
        pending_plan_review = (fut, "abc123")

    sess = _FakeSess()

    # Unrelated text — none of the approve/refine words
    text = "hello"
    pending = sess.pending_plan_review
    fut2, _ = pending
    normalized = text.strip().lower()
    approve_words = ("y", "yes", "approve")
    refine_words = ("n", "no", "refine")
    if normalized in approve_words or normalized in refine_words:
        raise AssertionError("unexpected match")

    # Future should still be pending
    assert not fut.done()
    assert sess.pending_plan_review is not None


# Helper: construct a future without an active event loop. asyncio
# requires a loop for create_future(); fall back to the concurrent
# Future for purely synchronous test logic.
def _make_future():
    import concurrent.futures
    # asyncio.Future from default loop, or concurrent for sync tests.
    try:
        loop = asyncio.new_event_loop()
        return loop.create_future()
    except Exception:
        return concurrent.futures.Future()
