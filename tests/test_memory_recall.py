"""Tests for janus.memory_recall (v1.18.0 Phase 3).

Covers: top_k pipeline, FTS5 ranking + recency rerank, scope filter,
phantom guard, budget enforcement, recall logging, top_k_block format.
"""

from __future__ import annotations
import datetime as _dt
import json
from pathlib import Path

import pytest

from janus import config, memory_cards, memory_index, memory_recall, session_context


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    monkeypatch.setattr(config, "MEMORY_RECALLS_LOG", home / "memory" / "recalls.jsonl")
    # Clear the per-process reconcile flag so each test gets a fresh sync.
    memory_recall.reset_reconcile_flag()
    # Clear origin so each test starts neutral.
    session_context.clear_origin()
    yield home
    session_context.clear_origin()


def _write_card(content: str, *, type: str = "preference",
                subject: str | None = None, scope: str = "global",
                durability: float = 0.5, confidence: float = 0.5,
                importance: float = 0.5,
                when: _dt.datetime | None = None) -> memory_cards.Card:
    c = memory_cards.make_card(
        type=type, subject=subject or content[:20],
        content=content,
        confidence=confidence, importance=importance, durability=durability,
        scope=scope, when=when,
    )
    memory_cards.write_card(c)
    return c


# ---------- top_k() pipeline ----------


class TestTopK:
    def test_empty_query_returns_empty(self, isolated_home):
        _write_card("some content", subject="x")
        assert memory_recall.top_k("") == []
        assert memory_recall.top_k("   ") == []

    def test_no_cards_returns_empty(self, isolated_home):
        assert memory_recall.top_k("anything") == []

    def test_basic_match(self, isolated_home):
        _write_card("I prefer my coffee black", subject="coffee")
        cards = memory_recall.top_k("coffee", current_scope="global")
        assert len(cards) == 1
        assert cards[0]["subject"] == "coffee"
        assert "_line" in cards[0]

    def test_top_k_caps_results(self, isolated_home):
        for i in range(10):
            _write_card(f"keyword content number {i}", subject=f"sub_{i}")
        cards = memory_recall.top_k("keyword", current_scope="global", top_k=3)
        assert len(cards) == 3

    def test_recency_decay_prefers_newer(self, isolated_home):
        # Same content, two cards: one from today, one from 60 days ago.
        old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=60)
        new = _dt.datetime.now(_dt.timezone.utc)
        _write_card("the keyword content here", subject="old", when=old)
        _write_card(
            "the keyword content here also",
            subject="new",
            when=new,
        )
        cards = memory_recall.top_k("keyword", current_scope="global")
        assert len(cards) == 2
        # Newer should rank first
        assert cards[0]["subject"] == "new"

    def test_phantom_guard_skips_missing_files(self, isolated_home):
        c = _write_card("phantom content", subject="x")
        # Reconcile populates the index
        memory_index.reconcile()
        # User manually deletes the card file
        memory_cards.card_path(c.id).unlink()
        # top_k must NOT return the phantom (path.exists() guard)
        cards = memory_recall.top_k("phantom", current_scope="global")
        assert cards == []

    def test_scope_filter_global_visible_everywhere(self, isolated_home):
        _write_card("public fact", subject="x", scope="global")
        # From any scope, global card visible
        for current in ["cli", "telegram:42", "web:s_a"]:
            cards = memory_recall.top_k("public", current_scope=current)
            assert len(cards) == 1

    def test_scope_filter_telegram_isolated(self, isolated_home):
        _write_card("private to chat 42", subject="x", scope="telegram:42")
        # Same chat: visible
        cards = memory_recall.top_k(
            "private", current_scope="telegram:42",
        )
        assert len(cards) == 1
        # Different telegram chat: not visible
        cards = memory_recall.top_k(
            "private", current_scope="telegram:99",
        )
        assert cards == []
        # CLI: not visible
        cards = memory_recall.top_k(
            "private", current_scope="cli",
        )
        assert cards == []

    def test_scope_filter_web_isolated(self, isolated_home):
        _write_card("session-specific", subject="x", scope="web:abc")
        cards = memory_recall.top_k(
            "session", current_scope="web:abc",
        )
        assert len(cards) == 1
        cards = memory_recall.top_k(
            "session", current_scope="web:xyz",
        )
        assert cards == []

    def test_scope_filter_project_descendant_match(self, isolated_home, tmp_path):
        proj = tmp_path / "myproj"
        proj.mkdir()
        sub = proj / "src" / "deeper"
        sub.mkdir(parents=True)
        _write_card(
            "project fact", subject="x", scope=f"project:{proj}",
        )
        # CWD inside project: card visible
        cards = memory_recall.top_k(
            "project", current_scope="cli", cwd=sub,
        )
        assert len(cards) == 1
        # CWD outside project: not visible
        cards = memory_recall.top_k(
            "project", current_scope="cli", cwd=tmp_path,
        )
        assert cards == []

    def test_budget_caps_total_bytes(self, isolated_home):
        # Insert 5 cards with long-ish content so all 5 won't fit in 200 bytes.
        for i in range(5):
            _write_card(
                "x" * 200 + f" tag {i}",  # each line ~200+ chars rendered
                subject=f"sub_{i}",
            )
        cards = memory_recall.top_k(
            "tag",
            current_scope="global",
            top_k=5,
            budget_bytes=200,
        )
        # Budget: at least one card returned (we always honor first hit),
        # but total bytes shouldn't exceed budget by much.
        assert len(cards) >= 1
        assert len(cards) < 5

    def test_budget_always_includes_first_hit(self, isolated_home):
        # Even with absurdly tight budget, the highest-ranked single card
        # is always returned.
        _write_card("a" * 500, subject="huge")
        cards = memory_recall.top_k(
            "huge",
            current_scope="global",
            top_k=5,
            budget_bytes=10,
        )
        assert len(cards) == 1


# ---------- top_k_block() rendering ----------


class TestTopKBlock:
    def test_empty_when_no_matches(self, isolated_home):
        assert memory_recall.top_k_block("zebra") == ""
        assert memory_recall.top_k_block("") == ""

    def test_block_starts_with_relevant_memories_header(self, isolated_home):
        _write_card("my coffee preference is black", subject="coffee")
        block = memory_recall.top_k_block(
            "coffee", current_scope="global",
        )
        assert block.startswith("## Relevant memories")

    def test_block_includes_type_subject_in_each_line(self, isolated_home):
        _write_card("morning ritual: long walk", subject="walking",
                    type="habit")
        block = memory_recall.top_k_block(
            "walk", current_scope="global",
        )
        assert "[habit:walking]" in block

    def test_block_truncates_long_content(self, isolated_home):
        long = "anchor word here " + ("x" * 500)
        _write_card(long, subject="long")
        block = memory_recall.top_k_block(
            "anchor", current_scope="global",
        )
        # Body should be truncated; ellipsis present
        assert "…" in block
        # Block stays under generous bound
        assert len(block) < 1500


# ---------- Recall logging ----------


class TestRecallLogging:
    def test_recall_appends_to_jsonl(self, isolated_home):
        _write_card("loggable content", subject="x")
        memory_recall.top_k_block("loggable", current_scope="global")
        log = config.MEMORY_DIR / "recalls.jsonl"
        assert log.exists()
        rows = [
            json.loads(line)
            for line in log.read_text("utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        rec = rows[0]
        assert "ts" in rec
        assert rec["query"] == "loggable"
        assert rec["scope"] == "global"
        assert len(rec["card_ids"]) == 1

    def test_no_log_when_no_match(self, isolated_home):
        _write_card("anything", subject="x")
        memory_recall.top_k_block("zebra", current_scope="global")
        log = config.MEMORY_DIR / "recalls.jsonl"
        # No row written when zero matches
        assert not log.exists() or log.read_text("utf-8").strip() == ""

    def test_recall_bumps_stats(self, isolated_home):
        c = _write_card("stat-checked content", subject="x")
        memory_recall.top_k_block("stat", current_scope="global")
        stats = memory_index.get_stats(c.id)
        assert stats is not None
        assert stats["recall_count"] == 1
        assert stats["last_recalled"] is not None

    def test_repeat_recall_increments_stats(self, isolated_home):
        c = _write_card("counted content", subject="x")
        memory_recall.top_k_block("counted", current_scope="global")
        memory_recall.top_k_block("counted", current_scope="global")
        memory_recall.top_k_block("counted", current_scope="global")
        assert memory_index.get_stats(c.id)["recall_count"] == 3


# ---------- Recency decay helper ----------


class TestRecencyDecay:
    def test_today_full_weight(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        decay = memory_recall._recency_decay(created, now=now)
        assert decay >= 0.99  # essentially 1.0

    def test_30_days_half(self):
        now = _dt.datetime.now(_dt.timezone.utc)
        old = now - _dt.timedelta(days=30)
        decay = memory_recall._recency_decay(
            old.strftime("%Y-%m-%dT%H:%M:%SZ"), now=now,
        )
        # exp(-30/30) = exp(-1) ≈ 0.368
        assert 0.30 < decay < 0.45

    def test_far_future_no_negative_age(self):
        # If 'created' is in the future (clock skew / bad timestamp), age is
        # clamped at 0 → decay = 1.0.
        now = _dt.datetime.now(_dt.timezone.utc)
        future = now + _dt.timedelta(days=10)
        decay = memory_recall._recency_decay(
            future.strftime("%Y-%m-%dT%H:%M:%SZ"), now=now,
        )
        assert decay == 1.0

    def test_unparseable_created_falls_back_to_now(self):
        # Should not raise; treats malformed as fresh.
        now = _dt.datetime.now(_dt.timezone.utc)
        decay = memory_recall._recency_decay("not a date", now=now)
        assert decay >= 0.99


# ---------- Truncation ----------


class TestTruncate:
    def test_short_unchanged(self):
        assert memory_recall._truncate("short", 100) == "short"

    def test_long_truncated_with_ellipsis(self):
        out = memory_recall._truncate("a" * 200, 50)
        assert len(out) == 50
        assert out.endswith("…")

    def test_newlines_collapsed_to_spaces(self):
        out = memory_recall._truncate("line1\nline2\nline3", 100)
        assert "\n" not in out
        assert "line1 line2 line3" in out

    def test_empty_string(self):
        assert memory_recall._truncate("", 10) == ""
        assert memory_recall._truncate(None, 10) == ""
