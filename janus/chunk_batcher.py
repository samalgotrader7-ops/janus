"""
chunk_batcher.py — debounced chunk collector for streaming surfaces
(v1.35.2, Phase 9.6).

WHY:
Streaming responses from the model arrive token-by-token. Naive
forwarding to Telegram (editMessageText), Discord, or any
edit-in-place chat surface produces N HTTP requests per turn — a
chatty user with a long response generates 100+ edits, hits API
rate limits, and gets banned. Batching at ~250ms windows turns
those 100 edits into ~4-5 edits per second of streaming, well
under any platform's rate limit, while still feeling live.

THIS MODULE: pure debouncer. Tracks pending content + the time of
the last flush; emits the accumulated buffer when (a) the
debounce window has elapsed since the last flush AND new content
has arrived, OR (b) flush() is called explicitly (e.g., on
turn-end). Works independently of whether you're in async or sync
context — the caller decides when to call should_flush().

USAGE PATTERN (sync):

    from janus.chunk_batcher import ChunkBatcher
    batcher = ChunkBatcher(window_seconds=0.25)
    for chunk in stream:
        batcher.add(chunk)
        if batcher.should_flush():
            send_edit(batcher.flush())
    if batcher.has_pending():
        send_edit(batcher.flush())  # final flush

USAGE PATTERN (async):

    import asyncio
    batcher = ChunkBatcher(window_seconds=0.25)
    async for chunk in async_stream():
        batcher.add(chunk)
        if batcher.should_flush():
            await async_send(batcher.flush())
    if batcher.has_pending():
        await async_send(batcher.flush())

NOT in scope:
  * Concurrent flush — single-producer assumption
  * Per-chunk priority — all chunks treated equally
  * Adaptive window — fixed timer; future v1.35.x could adapt
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ChunkBatcher:
    """Debounced chunk collector. Single-producer."""

    window_seconds: float = 0.25
    _pending: str = ""
    # Sentinel: -1 means "never flushed". Distinguishes from
    # "flushed at clock = 0.0" which is a real timestamp.
    _last_flush_ts: float = -1.0
    _now_fn: callable = time.monotonic

    def add(self, chunk: str) -> None:
        """Append a chunk to the pending buffer."""
        if chunk:
            self._pending += chunk

    def has_pending(self) -> bool:
        return bool(self._pending)

    def should_flush(self) -> bool:
        """True when the buffer has content AND the window has
        elapsed since the last flush. Returns False on:
          * empty buffer (nothing to flush)
          * within debounce window (caller should keep accumulating)
        """
        if not self._pending:
            return False
        # First flush is immediate (sentinel: never flushed before).
        if self._last_flush_ts < 0:
            return True
        elapsed = self._now_fn() - self._last_flush_ts
        return elapsed >= self.window_seconds

    def flush(self) -> str:
        """Drain the buffer + reset the debounce timer.
        Returns the accumulated content."""
        out = self._pending
        self._pending = ""
        self._last_flush_ts = self._now_fn()
        return out

    def reset(self) -> None:
        """Drop pending content + reset the timer. Used between turns."""
        self._pending = ""
        self._last_flush_ts = -1.0
