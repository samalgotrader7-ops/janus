"""Tests for v1.37.3 — telegram server-side /goal auto-continue (Phase 10.1.3).

Coverage:
  * on_text drains sess._goal_next_prompt, capped by
    JANUS_GOAL_ITERS_PER_MSG (default 5)
  * each iteration calls _run_chat_turn with the queued prompt
  * loop stops when _goal_next_prompt is None
  * loop stops when cap is hit even if next prompt is still set
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from janus.gateways import telegram as tg


def _mk_update(chat_id: int, text: str):
    """Mock just enough of telegram.Update for on_text to run."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.title = None
    update.message.text = text
    update.message.message_id = 1
    update.message.reply_text = AsyncMock()
    return update


def _mk_ctx():
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.application = None
    return ctx


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _setup(monkeypatch, chat_id, queue_of_next_prompts):
    """Common fixture wiring: authorize chat, mark session greeted,
    patch _run_chat_turn to log calls + simulate the goal queue."""
    tg.SESSIONS.clear()
    monkeypatch.setattr(tg, "_is_authorized", lambda cid: True)

    call_log = []
    queue = list(queue_of_next_prompts)

    async def fake_run_chat_turn(update, ctx, chat_id_arg, sess_arg, req):
        call_log.append(req)
        sess_arg._goal_next_prompt = queue.pop(0) if queue else None

    monkeypatch.setattr(tg, "_run_chat_turn", fake_run_chat_turn)

    sess = tg._session(chat_id)
    sess.mark_greeted()
    return call_log


# ---------- behavior ----------


@pytest.mark.asyncio
async def test_on_text_no_goal_calls_run_chat_turn_once(monkeypatch, janus_home):
    """Pin: when no goal is set (queue is empty / first call yields
    no next_prompt), only the initial _run_chat_turn fires."""
    call_log = _setup(monkeypatch, chat_id=11, queue_of_next_prompts=[None])
    await tg.on_text(_mk_update(11, "hi"), _mk_ctx())
    assert call_log == ["hi"]


@pytest.mark.asyncio
async def test_on_text_chains_until_no_next_prompt(monkeypatch, janus_home):
    """Pin: queued next_prompts trigger chained _run_chat_turn calls
    in order, until the queue clears."""
    call_log = _setup(
        monkeypatch, chat_id=12,
        queue_of_next_prompts=["step2", "step3", None],
    )
    await tg.on_text(_mk_update(12, "start"), _mk_ctx())
    assert call_log == ["start", "step2", "step3"]


@pytest.mark.asyncio
async def test_on_text_caps_at_max_iters(monkeypatch, janus_home):
    """Pin: even if next_prompt keeps getting queued, the loop
    stops at JANUS_GOAL_ITERS_PER_MSG."""
    monkeypatch.setenv("JANUS_GOAL_ITERS_PER_MSG", "3")
    # Queue has 100 entries — shouldn't all run; cap should kick in
    call_log = _setup(
        monkeypatch, chat_id=13,
        queue_of_next_prompts=[f"s{i}" for i in range(100)],
    )
    await tg.on_text(_mk_update(13, "go"), _mk_ctx())
    # Initial "go" + 3 chained = 4 total
    assert len(call_log) == 4
    assert call_log[0] == "go"
    assert call_log[1:] == ["s0", "s1", "s2"]


@pytest.mark.asyncio
async def test_on_text_default_cap_is_5(monkeypatch, janus_home):
    monkeypatch.delenv("JANUS_GOAL_ITERS_PER_MSG", raising=False)
    call_log = _setup(
        monkeypatch, chat_id=14,
        queue_of_next_prompts=[f"s{i}" for i in range(100)],
    )
    await tg.on_text(_mk_update(14, "go"), _mk_ctx())
    # Initial + 5 chained = 6
    assert len(call_log) == 6


@pytest.mark.asyncio
async def test_on_text_invalid_iter_env_falls_back_to_5(monkeypatch, janus_home):
    """Pin: garbage in JANUS_GOAL_ITERS_PER_MSG doesn't break on_text;
    falls back to default 5."""
    monkeypatch.setenv("JANUS_GOAL_ITERS_PER_MSG", "not-a-number")
    call_log = _setup(
        monkeypatch, chat_id=15,
        queue_of_next_prompts=[f"s{i}" for i in range(100)],
    )
    await tg.on_text(_mk_update(15, "go"), _mk_ctx())
    assert len(call_log) == 6  # initial + 5


@pytest.mark.asyncio
async def test_on_text_empty_input_returns_early(monkeypatch, janus_home):
    """Pin: empty user message doesn't trigger _run_chat_turn at
    all, so the goal loop also doesn't fire."""
    call_log = _setup(monkeypatch, chat_id=16, queue_of_next_prompts=[])
    await tg.on_text(_mk_update(16, "  "), _mk_ctx())
    assert call_log == []


# ---------- version ----------


def test_version_bumped_to_1_37_3():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 37, 3)
