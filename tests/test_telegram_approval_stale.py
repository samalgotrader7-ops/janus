"""Regression tests for v1.18.1 — Telegram approval-button silent-failure fix.

Pre-v1.18.1 bug (Sam's screenshot 2026-05-06):
    1. User receives an approval keyboard.
    2. 180s passes — wait_for cancels the future, approver returns
       False, the agent moves on (denied).
    3. User finally clicks a button.
    4. on_callback finds fut.done() → returns SILENTLY.
    5. From the user's POV, the buttons "do nothing".

The fix:
    - Bump approval timeout 180s → 1800s (30 min)
    - When on_callback finds a stale future, send a popup alert
      explaining the prompt expired
    - Edit the keyboard message to "(expired)"
    - On approver-side timeout, send a follow-up message in chat
"""

from __future__ import annotations
import asyncio
import inspect

import pytest

from janus.gateways import telegram as tg


# ---------- Source-level invariants (cheap regression guard) ----------


def test_approval_timeout_is_at_least_5_minutes():
    """3-minute timeout was the pre-fix default — would re-introduce the
    silent-click bug. Anything under 5 min is suspicious."""
    assert tg.APPROVAL_TIMEOUT_S >= 300


def test_clarify_timeout_is_at_least_5_minutes():
    assert tg.CLARIFY_TIMEOUT_S >= 300


def test_approval_timeout_env_overridable(monkeypatch):
    """Env var lets users tighten the window if they want — important for
    headless-bot deployments where they don't want stale prompts queued."""
    monkeypatch.setenv("JANUS_TELEGRAM_APPROVAL_TIMEOUT", "60")
    import importlib
    importlib.reload(tg)
    assert tg.APPROVAL_TIMEOUT_S == 60
    # Restore — other tests may rely on the default.
    monkeypatch.delenv("JANUS_TELEGRAM_APPROVAL_TIMEOUT")
    importlib.reload(tg)


def test_on_callback_handles_stale_future():
    """Source-level check that on_callback handles the done()/missing
    future case with visible feedback (not silent return).
    Pre-fix: bare `return` after `if fut is None or fut.done():`. Post-fix:
    must call q.answer() with text + show_alert."""
    src = inspect.getsource(tg.on_callback)
    # The stale-click branch must include show_alert=True (popup the user sees)
    assert "show_alert=True" in src, (
        "on_callback must show an alert popup when fut is stale "
        "(otherwise users see no feedback when they click an expired prompt)"
    )
    # And must mention "expired" so the user understands why
    assert "expired" in src.lower()


def test_approver_clears_future_on_timeout():
    """After the approver times out, the future entry MUST be removed from
    sess.approval_futures so a late click hits the missing-token branch
    (which now shows a popup). Pre-fix bug: entry was left behind, late
    click hit the done()-future branch and silently returned."""
    src = inspect.getsource(tg._make_approver)
    # The TimeoutError branch must pop the future from the dict
    assert "approval_futures.pop" in src
    # And mention APPROVAL_TIMEOUT_S (no more hard-coded 180/185)
    assert "APPROVAL_TIMEOUT_S" in src


def test_no_more_180s_hardcoded_timeout():
    """Regression — pre-fix had a hardcoded 180/185s timeout that caused
    the silent-failure bug."""
    src = inspect.getsource(tg._make_approver)
    # No literal 180 or 185 (the hard-coded magic numbers)
    # Check for "= 180" / "= 185" patterns or "timeout=180" specifically
    assert "timeout=180" not in src
    assert "timeout=185" not in src


def test_clarify_callback_uses_constant():
    src = inspect.getsource(tg._make_telegram_clarify_cb)
    assert "CLARIFY_TIMEOUT_S" in src
    # And no hardcoded 300
    assert "timeout=300" not in src


def test_clarify_sends_expiry_message_on_timeout():
    """When the clarify prompt times out, user should be told the agent
    proceeded without an answer — not just silence."""
    src = inspect.getsource(tg._make_telegram_clarify_cb)
    assert "expired" in src.lower()


# ---------- on_callback behavior with mocked PTB ----------


class _FakeMessage:
    def __init__(self, text="message text"):
        self.text = text


class _FakeQuery:
    def __init__(self, data: str, msg_text: str = "approval prompt"):
        self.data = data
        self.message = _FakeMessage(msg_text)
        self.answers: list[dict] = []
        self.edits: list[str] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text: str):
        self.edits.append(text)


class _FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, q: _FakeQuery, chat_id: int = 99):
        self.callback_query = q
        self.effective_chat = _FakeChat(chat_id)
        self.effective_user = None


class _FakeCtx:
    application = None


def _approve_chat(chat_id: int) -> None:
    """Make `is_authorized` return True for this chat — write directly
    to the approved-chats file (bypasses the pairing-code flow)."""
    from janus.gateways import _common as gw
    approved = gw._load_approved()
    approved.setdefault("telegram", [])
    if str(chat_id) not in approved["telegram"]:
        approved["telegram"].append(str(chat_id))
    gw._save_approved(approved)


@pytest.fixture
def isolate(janus_home):
    """Each test starts with a fresh SESSIONS dict + auth-approved chat."""
    tg.SESSIONS.clear()
    yield
    tg.SESSIONS.clear()


def test_stale_approval_click_shows_popup_and_edits_message(isolate):
    """Sam's actual bug: click after timeout → buttons do nothing.

    Setup: future is registered, then explicitly cancelled (simulating
    timeout). User clicks — must see a popup AND a message edit, not
    silent return.
    """
    chat_id = 99
    _approve_chat(chat_id)
    sess = tg._session(chat_id)

    # Set up a future and immediately cancel it — simulates the 180s
    # timeout having fired.
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        sess.approval_futures["abc12345"] = fut
        sess.approval_futures["abc12345.key"] = "shell.exec"
        fut.cancel()
        assert fut.done()  # precondition for the bug

        q = _FakeQuery("appr:abc12345:always")
        update = _FakeUpdate(q, chat_id=chat_id)
        loop.run_until_complete(tg.on_callback(update, _FakeCtx()))
    finally:
        loop.close()

    # The user MUST get a popup explaining what happened.
    assert q.answers, "no q.answer() call — user got no feedback"
    last = q.answers[-1]
    assert last["show_alert"] is True
    assert "expired" in (last["text"] or "").lower()
    # AND the keyboard message should be edited so the buttons are
    # visibly dead.
    assert q.edits, "no edit_message_text — keyboard still looks live"
    assert "expired" in q.edits[-1].lower()


def test_unauthorized_callback_shows_popup(isolate):
    """Auth check failure must give the user a popup, not silent return."""
    # Don't approve the chat → auth fails
    chat_id = 12345
    sess = tg._session(chat_id)
    fut = asyncio.new_event_loop().create_future()
    sess.approval_futures["xyz98765"] = fut

    q = _FakeQuery("appr:xyz98765:always")
    update = _FakeUpdate(q, chat_id=chat_id)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(tg.on_callback(update, _FakeCtx()))
    finally:
        loop.close()
    assert q.answers
    assert q.answers[-1]["show_alert"] is True
    assert "not authorized" in (q.answers[-1]["text"] or "").lower()


def test_successful_click_resolves_future_and_pops_alert(isolate):
    """Happy path — verify the popup-feedback was added without breaking
    the working flow."""
    chat_id = 99
    _approve_chat(chat_id)
    sess = tg._session(chat_id)
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        sess.approval_futures["okok1234"] = fut
        sess.approval_futures["okok1234.key"] = "fs.write"

        q = _FakeQuery("appr:okok1234:always")
        update = _FakeUpdate(q, chat_id=chat_id)
        loop.run_until_complete(tg.on_callback(update, _FakeCtx()))

        # Future was resolved with True
        assert fut.done()
        assert fut.result() is True
    finally:
        loop.close()
    # User saw popup feedback
    assert q.answers
    assert "always" in (q.answers[-1]["text"] or "").lower()
    # And an edit
    assert q.edits


def test_clarify_stale_click_shows_popup(isolate):
    """Same bug class on the clarify keyboard (model asks user a question)."""
    chat_id = 99
    _approve_chat(chat_id)
    sess = tg._session(chat_id)
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        sess.clarify_futures["clr_abc"] = fut
        fut.cancel()

        q = _FakeQuery("clr:clr_abc:0")
        update = _FakeUpdate(q, chat_id=chat_id)
        loop.run_until_complete(tg.on_callback(update, _FakeCtx()))
    finally:
        loop.close()
    assert q.answers
    assert "expired" in (q.answers[-1]["text"] or "").lower()
