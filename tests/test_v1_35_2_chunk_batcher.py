"""Tests for v1.35.2 — chunk batcher (Phase 9.6)."""

from __future__ import annotations

import pytest

from janus.chunk_batcher import ChunkBatcher


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t
    def __call__(self):
        return self.t


def test_empty_batcher_no_flush():
    b = ChunkBatcher()
    assert b.has_pending() is False
    assert b.should_flush() is False


def test_first_flush_immediate():
    """First chunk should flush immediately (no prior flush)."""
    b = ChunkBatcher(window_seconds=0.25)
    b.add("hello")
    assert b.should_flush() is True


def test_flush_returns_pending_and_resets():
    b = ChunkBatcher()
    b.add("hello ")
    b.add("world")
    out = b.flush()
    assert out == "hello world"
    assert b.has_pending() is False


def test_after_flush_within_window_no_flush():
    """Within the debounce window, should_flush returns False."""
    clock = FakeClock(0.0)
    b = ChunkBatcher(window_seconds=0.25, _now_fn=clock)
    b.add("a")
    b.flush()  # last_flush_ts = 0.0
    b.add("b")
    clock.t = 0.1  # 100ms < 250ms window
    assert b.should_flush() is False


def test_after_window_elapsed_flush_true():
    clock = FakeClock(0.0)
    b = ChunkBatcher(window_seconds=0.25, _now_fn=clock)
    b.add("a")
    b.flush()
    b.add("b")
    clock.t = 0.30  # 300ms > 250ms window
    assert b.should_flush() is True


def test_no_pending_after_flush_returns_false():
    """should_flush is False when buffer is empty even if window
    has elapsed — nothing to flush."""
    clock = FakeClock(0.0)
    b = ChunkBatcher(window_seconds=0.25, _now_fn=clock)
    b.add("a")
    b.flush()
    clock.t = 1.0  # plenty of time elapsed
    assert b.should_flush() is False


def test_add_empty_chunk_ignored():
    b = ChunkBatcher()
    b.add("")
    assert b.has_pending() is False


def test_reset_clears_state():
    b = ChunkBatcher()
    b.add("data")
    b.flush()
    b.reset()
    # After reset, should_flush behavior is back to "first flush"
    b.add("new")
    assert b.should_flush() is True


def test_realistic_stream_pattern():
    """Simulate a stream: 10 tokens spread over 1 second.
    With 250ms window, should fire ~4 batches."""
    clock = FakeClock(0.0)
    b = ChunkBatcher(window_seconds=0.25, _now_fn=clock)
    flushes = []

    tokens = ["t" + str(i) for i in range(10)]
    for i, tok in enumerate(tokens):
        clock.t = i * 0.1  # token every 100ms
        b.add(tok)
        if b.should_flush():
            flushes.append(b.flush())
    if b.has_pending():
        flushes.append(b.flush())

    # 10 tokens, 250ms window over 1 second = ~5 batches max,
    # ~4 minimum (first flush immediate then 250ms gates).
    assert 3 <= len(flushes) <= 6
    # All content preserved across flushes
    assert "".join(flushes) == "".join(tokens)


def test_version_bumped_to_1_35_2_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 2)
