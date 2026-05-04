"""Tests for v1.5.1 phase 4: Telegram continuous typing indicator.

Bug: When Janus took a long time (multiple tool calls), the Telegram
bot looked frozen. No "typing…" indicator. Sam thought the bot wasn't
working.

Fix: pulse `bot.send_chat_action("typing")` every 4s while the chat
turn is in flight. Telegram displays "typing…" dots until the next
pulse expires (~5s), so a 4s interval keeps it continuous.
"""
from __future__ import annotations
import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from janus.gateways import telegram


# ---------- _typing_pulse exists ----------


def test_typing_pulse_function_exists():
    assert hasattr(telegram, "_typing_pulse")
    assert inspect.iscoroutinefunction(telegram._typing_pulse)


def test_run_chat_turn_starts_typing_pulse():
    """Source-level pin: _run_chat_turn must launch the pulse task
    BEFORE invoking executor.chat."""
    src = inspect.getsource(telegram._run_chat_turn)
    assert "_typing_pulse" in src
    # Find the actual to_thread(executor.chat, ...) call — not the
    # docstring mention.
    call_marker = "asyncio.to_thread(\n            executor.chat,"
    chat_idx = src.find(call_marker)
    assert chat_idx >= 0, "could not find the executor.chat invocation"
    pulse_idx = src.rfind("_typing_pulse", 0, chat_idx)
    assert pulse_idx >= 0, (
        "typing pulse must be started before the executor.chat invocation"
    )


def test_run_chat_turn_cancels_typing_pulse():
    """Source-level pin: pulse cancelled in finally block (so it stops
    even on exception)."""
    src = inspect.getsource(telegram._run_chat_turn)
    assert "typing_task.cancel()" in src
    # Should be in a finally block (defensive cleanup)
    assert "finally:" in src


# ---------- Pulse behavior ----------


@pytest.mark.asyncio
async def test_typing_pulse_calls_send_chat_action():
    """Pulse should call send_chat_action with action='typing' immediately."""
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()

    task = asyncio.create_task(telegram._typing_pulse(bot, 123, interval_s=0.05))
    await asyncio.sleep(0.02)  # let the first pulse fire
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert bot.send_chat_action.called
    call_kwargs = bot.send_chat_action.call_args.kwargs
    assert call_kwargs.get("chat_id") == 123
    assert call_kwargs.get("action") == "typing"


@pytest.mark.asyncio
async def test_typing_pulse_pulses_repeatedly():
    """Pulse should fire multiple times during a long operation."""
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()

    task = asyncio.create_task(telegram._typing_pulse(bot, 1, interval_s=0.05))
    await asyncio.sleep(0.18)  # ~3-4 pulses
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert bot.send_chat_action.call_count >= 2


@pytest.mark.asyncio
async def test_typing_pulse_swallows_send_exception():
    """Network blip during a pulse must not kill the loop — should
    sleep and try again next interval."""
    bot = MagicMock()
    call_count = {"n": 0}

    async def flaky(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("network blip")
        # Subsequent pulses succeed
    bot.send_chat_action = flaky

    task = asyncio.create_task(telegram._typing_pulse(bot, 1, interval_s=0.05))
    await asyncio.sleep(0.18)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # First pulse raised but the loop continued; later pulses ran
    assert call_count["n"] >= 2


@pytest.mark.asyncio
async def test_typing_pulse_exits_cleanly_on_cancel():
    """Pulse uses asyncio.CancelledError as the termination signal."""
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()

    task = asyncio.create_task(telegram._typing_pulse(bot, 1, interval_s=10))
    # Cancel almost immediately
    await asyncio.sleep(0.01)
    task.cancel()
    # Should NOT raise on await
    await task  # If this raises CancelledError, pytest will fail
    # (The pulse swallows CancelledError internally and returns.)
