"""Tests for the v1.0 Telegram gateway.

Coverage:
- per-chat Session keeps a messages list across messages
- mode-aware approver: ALLOW returns True silently, DENY returns False
- /mode command switches the session's mode
- on_text runs executor.chat() and accumulates messages

We don't spin up a real Telegram bot — we instantiate the in-process
state objects and call the approver directly. The python-telegram-bot
import path is exercised by the import smoke at the top.
"""
from __future__ import annotations

import pytest

from janus import permissions
from janus.gateways import telegram as tg


# ---------- Session basics ----------


def test_session_starts_with_empty_messages_and_default_mode(janus_home):
    s = tg.Session(chat_id=42)
    assert s.messages == []
    assert s.mode_state.current == permissions.DEFAULT


def test_session_singleton_per_chat_id(janus_home):
    tg.SESSIONS.clear()
    a = tg._session(7)
    b = tg._session(7)
    assert a is b
    c = tg._session(8)
    assert c is not a


# ---------- Mode-aware approver ----------


def test_approver_allow_returns_true_silently(janus_home):
    sess = tg.Session(chat_id=1)
    sess.mode_state.set(permissions.DEFAULT)
    approver = tg._make_approver(chat_id=1, app=None, sess=sess)
    # read risk under default mode → ALLOW.
    assert approver("fs_read", "details", risk="read") is True


def test_approver_deny_returns_false_silently(janus_home):
    sess = tg.Session(chat_id=1)
    sess.mode_state.set(permissions.PLAN)
    approver = tg._make_approver(chat_id=1, app=None, sess=sess)
    # write risk under plan mode → DENY.
    assert approver("fs_write", "details", risk="write") is False


def test_approver_bypass_mode_allows_exec_without_keyboard(janus_home):
    sess = tg.Session(chat_id=1)
    sess.mode_state.set(permissions.BYPASS)
    approver = tg._make_approver(chat_id=1, app=None, sess=sess)
    assert approver("shell exec", "details", risk="exec") is True


def test_approver_falls_back_to_verb_when_risk_missing(janus_home):
    """Legacy callers that don't pass risk= should still get the right
    decision based on the capability verb."""
    sess = tg.Session(chat_id=1)
    sess.mode_state.set(permissions.PLAN)
    approver = tg._make_approver(chat_id=1, app=None, sess=sess)
    # capability verb=write → write risk → PLAN denies.
    out = approver("fs_write", "details", capability=("fs", "write", "x"))
    assert out is False


# ---------- /mode behavior on Session.mode_state ----------


def test_mode_switch_via_session_state(janus_home):
    s = tg.Session(chat_id=1)
    assert s.mode_state.current == permissions.DEFAULT
    s.mode_state.set("acceptEdits")
    assert s.mode_state.current == permissions.ACCEPT_EDITS
    s.mode_state.set("plan")
    assert s.mode_state.current == permissions.PLAN


def test_mode_legacy_name_normalizes(janus_home):
    s = tg.Session(chat_id=1)
    s.mode_state.set("auto")
    assert s.mode_state.current == permissions.BYPASS


# ---------- chat() integration ----------


def _stub_llm(monkeypatch, responses):
    import janus.llm
    queue = list(responses)

    def chat(messages, tools=None, json_mode=False, temperature=0.7):
        if not queue:
            raise RuntimeError("stub queue empty")
        return queue.pop(0)

    monkeypatch.setattr(janus.llm, "chat", chat)
    return queue


def test_chat_loop_accumulates_messages_across_two_turns(janus_home, monkeypatch):
    """Direct test of the executor.chat() integration: per-chat messages
    list grows monotonically across two messages from the same chat."""
    from janus import executor
    from janus.tools.base import Registry

    _stub_llm(monkeypatch, [
        {"role": "assistant", "content": "first"},
        {"role": "assistant", "content": "second"},
    ])
    sess = tg.Session(chat_id=1)
    approver = tg._make_approver(chat_id=1, app=None, sess=sess)

    out1, _ = executor.chat(
        messages=sess.messages, user_input="hello",
        tools=Registry([]), approver=approver, stream=False,
    )
    assert out1 == "first"

    out2, _ = executor.chat(
        messages=sess.messages, user_input="again",
        tools=Registry([]), approver=approver, stream=False,
    )
    assert out2 == "second"

    # system + user1 + assistant1 + user2 + assistant2
    assert len(sess.messages) == 5
    assert sess.messages[1]["content"] == "hello"
    assert sess.messages[3]["content"] == "again"


# ---------- No interpretation picker (regression for the v1.0 pivot) ----------


def test_no_interpretation_callback_handler_remains(janus_home):
    """Regression: pre-v1.0 had `interp:N` callback data for picker taps.
    v1.0 only has `appr:*` for approval. The handler should branch on
    appr: prefix and not on interp:."""
    import inspect
    src = inspect.getsource(tg.on_callback)
    # Strip the docstring to check live code only.
    body = src.split('"""', 2)[-1] if '"""' in src else src
    assert "interp:" not in body
    assert "appr:" in body
    # Also: no _execute_with_choice helper — that was the picker bridge.
    assert not hasattr(tg, "_execute_with_choice")
