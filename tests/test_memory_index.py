"""Tests for janus.memory_index (v1.18.0 Phase 2).

Covers: schema creation, reconcile (new/drifted/phantom/unchanged),
FTS5 query + ranking, lookup helpers, recall stats, summary, reset.
"""

from __future__ import annotations
import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from janus import config, memory_cards, memory_index


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    return home


def _write(content: str, *, type: str = "preference", subject: str | None = None,
           scope: str = "global", durability: float = 0.5,
           confidence: float = 0.5, importance: float = 0.5,
           when: _dt.datetime | None = None) -> memory_cards.Card:
    """Helper: build + write a card; return the Card."""
    c = memory_cards.make_card(
        type=type, subject=subject or content[:20] or "x",
        content=content,
        confidence=confidence, importance=importance, durability=durability,
        scope=scope, when=when,
    )
    memory_cards.write_card(c)
    return c


# ---------- Schema ----------


class TestSchema:
    def test_connect_creates_db_and_tables(self, isolated_home):
        conn = memory_index._connect()
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            # FTS5 creates extra shadow tables — just check our 3 are there.
            assert "cards_seen" in tables
            assert "stats" in tables
            # cards_fts is a virtual table; sqlite_master records it differently
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='cards_fts'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_db_lives_at_expected_path(self, isolated_home):
        memory_index._connect().close()
        assert (config.MEMORY_DIR / "index.db").exists()


# ---------- Reconcile ----------


class TestReconcile:
    def test_empty_when_no_cards(self, isolated_home):
        counts = memory_index.reconcile()
        assert counts == {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}

    def test_inserts_new_cards(self, isolated_home):
        _write("first card content", subject="alpha")
        _write("second card content", subject="beta")
        counts = memory_index.reconcile()
        assert counts["added"] == 2
        assert counts["updated"] == 0
        assert counts["deleted"] == 0

    def test_unchanged_cards_skipped(self, isolated_home):
        _write("card content", subject="x")
        memory_index.reconcile()  # first pass: added=1
        counts = memory_index.reconcile()  # second pass: unchanged=1
        assert counts["added"] == 0
        assert counts["unchanged"] == 1
        assert counts["updated"] == 0

    def test_drifted_card_updated(self, isolated_home):
        c = _write("original content", subject="alpha")
        memory_index.reconcile()
        # Hand-edit the file body to simulate drift (user edits markdown
        # directly). Keep the same id but change content.
        path = memory_cards.card_path(c.id)
        text = path.read_text("utf-8")
        # Replace the body content (after the frontmatter close).
        new_text = text.replace("original content", "MUTATED content")
        path.write_text(new_text, encoding="utf-8")

        counts = memory_index.reconcile()
        assert counts["updated"] == 1
        assert counts["unchanged"] == 0

        # FTS picks up the new content.
        hits = memory_index.query_fts("MUTATED")
        assert len(hits) == 1
        assert hits[0]["id"] == c.id

    def test_phantom_card_deleted(self, isolated_home):
        c = _write("about to vanish", subject="x")
        memory_index.reconcile()  # in DB
        # User manually deletes the markdown file.
        memory_cards.card_path(c.id).unlink()

        counts = memory_index.reconcile()
        assert counts["deleted"] == 1
        # Lookup confirms it's gone.
        assert memory_index.lookup_by_id(c.id) is None

    def test_malformed_card_skipped_not_crashed(self, isolated_home):
        # Drop a malformed file directly in cards/.
        config.MEMORY_CARDS_DIR.mkdir(parents=True, exist_ok=True)
        bad = config.MEMORY_CARDS_DIR / "bad-id.md"
        bad.write_text("this isn't a valid card", encoding="utf-8")
        # Plus a good one
        _write("good content", subject="x")

        counts = memory_index.reconcile()
        # bad one skipped; good one added
        assert counts["added"] == 1

    def test_idempotent(self, isolated_home):
        _write("a", subject="alpha")
        _write("b", subject="beta")
        first = memory_index.reconcile()
        second = memory_index.reconcile()
        third = memory_index.reconcile()
        assert first["added"] == 2
        assert second["added"] == 0
        assert third == second


# ---------- FTS5 query ----------


class TestQueryFts:
    def test_empty_query_returns_empty(self, isolated_home):
        _write("some content", subject="x")
        memory_index.reconcile()
        assert memory_index.query_fts("") == []
        assert memory_index.query_fts("   ") == []

    def test_basic_keyword_match(self, isolated_home):
        _write("I like coffee black no sugar", subject="coffee")
        _write("I prefer my tea with milk", subject="tea")
        memory_index.reconcile()

        hits = memory_index.query_fts("coffee")
        assert len(hits) == 1
        assert hits[0]["subject"] == "coffee"

    def test_multi_word_or_match(self, isolated_home):
        _write("git pull rebase main", subject="git_workflow")
        _write("python pytest tooling", subject="python")
        memory_index.reconcile()

        # Multi-word query OR-joined: matches either word.
        hits = memory_index.query_fts("git python")
        assert len(hits) == 2

    def test_no_match_returns_empty(self, isolated_home):
        _write("alpha beta gamma", subject="x")
        memory_index.reconcile()
        assert memory_index.query_fts("zebra") == []

    def test_query_includes_subject_field(self, isolated_home):
        # Subject is FTS-indexed, so a subject-word should match.
        _write("totally unrelated body text", subject="elephant")
        memory_index.reconcile()
        hits = memory_index.query_fts("elephant")
        assert len(hits) == 1

    def test_score_higher_for_more_matches(self, isolated_home):
        # Card mentioning "coffee" multiple times should score higher.
        _write("coffee mentioned briefly", subject="weak")
        _write(
            "coffee coffee coffee heavy emphasis on coffee",
            subject="strong",
        )
        memory_index.reconcile()

        hits = memory_index.query_fts("coffee")
        assert len(hits) == 2
        # Higher score first (per ORDER BY DESC).
        assert hits[0]["score"] >= hits[1]["score"]

    def test_query_safe_with_punctuation(self, isolated_home):
        # FTS5 special chars in user input must not crash the query.
        _write("legitimate content", subject="x")
        memory_index.reconcile()
        # Should not raise.
        memory_index.query_fts('"weird" (chars*) :stuff')

    def test_query_returns_full_metadata(self, isolated_home):
        _write("hello world", subject="greeting", scope="cli",
               confidence=0.8, importance=0.5, durability=0.7)
        memory_index.reconcile()

        hits = memory_index.query_fts("hello")
        assert len(hits) == 1
        h = hits[0]
        assert h["subject"] == "greeting"
        assert h["scope"] == "cli"
        assert h["confidence"] == 0.8
        assert h["durability"] == 0.7
        assert "score" in h

    def test_limit_param(self, isolated_home):
        for i in range(10):
            _write(f"keyword content {i}", subject=f"sub_{i}")
        memory_index.reconcile()

        hits = memory_index.query_fts("keyword", limit=3)
        assert len(hits) == 3


# ---------- Lookup helpers ----------


class TestLookup:
    def test_lookup_by_id_hit(self, isolated_home):
        c = _write("hello", subject="greet", scope="global")
        memory_index.reconcile()
        row = memory_index.lookup_by_id(c.id)
        assert row is not None
        assert row["subject"] == "greet"
        assert row["scope"] == "global"

    def test_lookup_by_id_miss(self, isolated_home):
        assert memory_index.lookup_by_id("nonexistent") is None

    def test_lookup_by_subject_finds_collisions(self, isolated_home):
        _write("black no sugar", subject="coffee", type="preference",
               when=_dt.datetime(2026, 5, 1))
        _write("flat white double shot", subject="coffee", type="preference",
               when=_dt.datetime(2026, 5, 5))
        memory_index.reconcile()

        rows = memory_index.lookup_by_subject("preference", "coffee")
        assert len(rows) == 2
        # Newest first
        assert rows[0]["created"] > rows[1]["created"]

    def test_lookup_by_subject_type_specific(self, isolated_home):
        # Same subject, different types → not collisions.
        _write("morning routine", subject="coffee", type="habit")
        _write("strong dark", subject="coffee", type="preference")
        memory_index.reconcile()

        habit_rows = memory_index.lookup_by_subject("habit", "coffee")
        pref_rows = memory_index.lookup_by_subject("preference", "coffee")
        assert len(habit_rows) == 1
        assert len(pref_rows) == 1
        assert habit_rows[0]["id"] != pref_rows[0]["id"]

    def test_list_all_no_filter(self, isolated_home):
        _write("a", subject="alpha")
        _write("b", subject="beta")
        _write("c", subject="gamma")
        memory_index.reconcile()
        rows = memory_index.list_all()
        assert len(rows) == 3

    def test_list_all_scope_filter(self, isolated_home):
        _write("a", subject="alpha", scope="global")
        _write("b", subject="beta", scope="cli")
        _write("c", subject="gamma", scope="cli")
        memory_index.reconcile()

        rows = memory_index.list_all(scope="cli")
        assert len(rows) == 2
        assert all(r["scope"] == "cli" for r in rows)

    def test_list_all_type_filter(self, isolated_home):
        _write("a", subject="alpha", type="preference")
        _write("b", subject="beta", type="habit")
        _write("c", subject="gamma", type="habit")
        memory_index.reconcile()

        rows = memory_index.list_all(type="habit")
        assert len(rows) == 2
        assert all(r["type"] == "habit" for r in rows)


# ---------- Recall stats ----------


class TestStats:
    def test_bump_recall_creates_row(self, isolated_home):
        c = _write("x", subject="x")
        memory_index.reconcile()
        memory_index.bump_recall([c.id])
        st = memory_index.get_stats(c.id)
        assert st is not None
        assert st["recall_count"] == 1
        assert st["last_recalled"] is not None

    def test_bump_recall_increments_existing(self, isolated_home):
        c = _write("x", subject="x")
        memory_index.reconcile()
        memory_index.bump_recall([c.id])
        memory_index.bump_recall([c.id])
        memory_index.bump_recall([c.id])
        assert memory_index.get_stats(c.id)["recall_count"] == 3

    def test_bump_recall_empty_list_noop(self, isolated_home):
        # Should not raise.
        memory_index.bump_recall([])

    def test_bump_recall_explicit_when(self, isolated_home):
        c = _write("x", subject="x")
        memory_index.reconcile()
        memory_index.bump_recall([c.id], when="2026-05-05T12:00:00Z")
        assert memory_index.get_stats(c.id)["last_recalled"] == "2026-05-05T12:00:00Z"

    def test_get_stats_miss_returns_none(self, isolated_home):
        assert memory_index.get_stats("never-seen") is None

    def test_bump_recall_multiple_ids(self, isolated_home):
        c1 = _write("a", subject="alpha")
        c2 = _write("b", subject="beta")
        memory_index.reconcile()
        memory_index.bump_recall([c1.id, c2.id])
        assert memory_index.get_stats(c1.id)["recall_count"] == 1
        assert memory_index.get_stats(c2.id)["recall_count"] == 1


# ---------- Summary ----------


class TestSummary:
    def test_summary_empty(self, isolated_home):
        s = memory_index.summary()
        assert s["total"] == 0
        assert s["per_type"] == {}
        assert s["per_scope"] == {}
        assert s["total_recalls"] == 0
        assert s["most_recalled"] == []

    def test_summary_per_type_counts(self, isolated_home):
        _write("a", subject="x", type="preference")
        _write("b", subject="y", type="preference")
        _write("c", subject="z", type="habit")
        memory_index.reconcile()

        s = memory_index.summary()
        assert s["total"] == 3
        assert s["per_type"]["preference"] == 2
        assert s["per_type"]["habit"] == 1

    def test_summary_per_scope_counts(self, isolated_home):
        _write("a", subject="x", scope="global")
        _write("b", subject="y", scope="cli")
        memory_index.reconcile()

        s = memory_index.summary()
        assert s["per_scope"]["global"] == 1
        assert s["per_scope"]["cli"] == 1

    def test_summary_most_recalled(self, isolated_home):
        c1 = _write("a", subject="alpha")
        c2 = _write("b", subject="beta")
        memory_index.reconcile()
        # Recall c2 three times, c1 once.
        memory_index.bump_recall([c1.id])
        for _ in range(3):
            memory_index.bump_recall([c2.id])

        s = memory_index.summary()
        assert s["total_recalls"] == 4
        assert s["most_recalled"][0]["id"] == c2.id
        assert s["most_recalled"][0]["recall_count"] == 3


# ---------- P5 reset/rebuild seam ----------


class TestResetRebuild:
    def test_reset_deletes_db(self, isolated_home):
        _write("x", subject="x")
        memory_index.reconcile()
        assert (config.MEMORY_DIR / "index.db").exists()
        memory_index.reset()
        assert not (config.MEMORY_DIR / "index.db").exists()

    def test_reset_then_reconcile_rebuilds_full_state(self, isolated_home):
        # The P5 demo: rm index.db; reconcile() restores from cards/.
        cards = [
            _write("first content", subject="alpha"),
            _write("second content", subject="beta"),
            _write("third content", subject="gamma"),
        ]
        memory_index.reconcile()
        # Bump recall to verify stats survive across reset cycles.
        memory_index.bump_recall([cards[0].id])
        before = memory_index.summary()

        memory_index.reset()
        # Stats are gone (they live in DB).
        assert memory_index.get_stats(cards[0].id) is None

        # Reconcile rebuilds.
        counts = memory_index.reconcile()
        assert counts["added"] == 3

        after = memory_index.summary()
        # Counts match — but recall stats are gone (acceptable; they're
        # cache-only ephemera, will accumulate again).
        assert after["total"] == before["total"]
        assert after["per_type"] == before["per_type"]

        # Search still works.
        hits = memory_index.query_fts("first")
        assert len(hits) == 1
        assert hits[0]["subject"] == "alpha"

    def test_reset_when_db_missing_is_noop(self, isolated_home):
        # Should not raise.
        memory_index.reset()
        memory_index.reset()
