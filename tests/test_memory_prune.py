"""Tests for janus.memory_prune (v1.18.0 Phase 8).

Covers: active drop (21d, durability<0.3), low-conf drop (120d,
durability<0.5, confidence<0.4), permanent superseded cleanup (30d).
Protected (durability>=0.7) cards never drop.
"""

from __future__ import annotations
import datetime as _dt
from pathlib import Path

import pytest

from janus import config, memory_cards, memory_index, memory_prune


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    monkeypatch.setattr(config, "MEMORY_PROTECTED_DURABILITY", 0.7)
    monkeypatch.setattr(config, "MEMORY_PRUNE_ACTIVE_DAYS", 21)
    monkeypatch.setattr(config, "MEMORY_PRUNE_LOWCONF_DAYS", 120)
    monkeypatch.setattr(config, "MEMORY_PRUNE_LOWCONF_THRESHOLD", 0.4)
    monkeypatch.setattr(config, "MEMORY_PRUNE_SUPERSEDED_DAYS", 30)
    yield home


def _make(*, content: str, durability: float, confidence: float = 0.5,
          when: _dt.datetime, subject: str | None = None) -> memory_cards.Card:
    c = memory_cards.make_card(
        type="preference",
        subject=subject or content[:20],
        content=content,
        confidence=confidence,
        importance=0.5,
        durability=durability,
        scope="global",
        when=when,
    )
    memory_cards.write_card(c)
    return c


# ---------- Active-scope drop (21d, durability<0.3) ----------


class TestActiveDrop:
    def test_old_low_durability_dropped(self, isolated_home):
        # Created 22 days ago, durability=0.2 → should be active-dropped
        old_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=22)
        c = _make(content="ephemeral", durability=0.2, when=old_dt)
        memory_index.reconcile()

        counts = memory_prune.run_once()
        assert counts["active_drops"] == 1
        assert counts["removed"] == 1
        # Card is now in _superseded/
        assert not memory_cards.card_path(c.id).exists()
        sup = config.MEMORY_CARDS_DIR / "_superseded" / f"{c.id}.md"
        assert sup.exists()

    def test_recent_low_durability_kept(self, isolated_home):
        # 5 days old → still under the 21d threshold
        recent = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=5)
        c = _make(content="recent", durability=0.2, when=recent)
        memory_index.reconcile()

        counts = memory_prune.run_once()
        assert counts["active_drops"] == 0
        assert memory_cards.card_path(c.id).exists()

    def test_old_medium_durability_kept(self, isolated_home):
        # 22 days old but durability=0.5 → over the 0.3 threshold
        old_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=22)
        c = _make(content="medium", durability=0.5, when=old_dt)
        memory_index.reconcile()

        counts = memory_prune.run_once()
        assert counts["active_drops"] == 0
        assert memory_cards.card_path(c.id).exists()


# ---------- Low-confidence durable drop ----------


class TestLowConfDrop:
    def test_old_lowconf_dropped(self, isolated_home):
        # 130d old, durability=0.4, confidence=0.3 → low_conf drop
        old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=130)
        c = _make(content="uncertain", durability=0.4, confidence=0.3, when=old)
        memory_index.reconcile()

        counts = memory_prune.run_once()
        assert counts["low_conf_drops"] == 1
        assert counts["removed"] == 1

    def test_old_high_conf_kept(self, isolated_home):
        # 130d old, low durability, but HIGH confidence → kept
        old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=130)
        c = _make(content="confident", durability=0.4, confidence=0.9, when=old)
        memory_index.reconcile()

        counts = memory_prune.run_once()
        assert counts["low_conf_drops"] == 0
        assert memory_cards.card_path(c.id).exists()

    def test_lowconf_under_120d_kept(self, isolated_home):
        # 90d old, low conf → not yet over the 120d threshold
        recent = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=90)
        c = _make(content="x", durability=0.4, confidence=0.3, when=recent)
        memory_index.reconcile()

        counts = memory_prune.run_once()
        assert counts["low_conf_drops"] == 0


# ---------- Protected-durability invariant ----------


class TestProtectedInvariant:
    def test_protected_never_dropped_by_age(self, isolated_home):
        # Very old AND low confidence — but protected by durability
        ancient = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=500)
        c = _make(
            content="identity-class fact",
            durability=0.9,  # >= 0.7 = protected
            confidence=0.1,  # very low
            when=ancient,
        )
        memory_index.reconcile()

        counts = memory_prune.run_once()
        assert counts["removed"] == 0
        assert memory_cards.card_path(c.id).exists()

    def test_durability_threshold_inclusive(self, isolated_home):
        # durability == 0.7 → protected (>= threshold)
        ancient = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=500)
        c = _make(
            content="exact-threshold",
            durability=0.7,
            confidence=0.1,
            when=ancient,
        )
        memory_index.reconcile()

        counts = memory_prune.run_once()
        assert counts["removed"] == 0
        assert memory_cards.card_path(c.id).exists()


# ---------- Superseded permanent cleanup ----------


class TestSupersededCleanup:
    def test_old_superseded_unlinked(self, isolated_home):
        c = _make(
            content="x", durability=0.5,
            when=_dt.datetime.now(_dt.timezone.utc),
        )
        memory_cards.supersede(c.id)
        sup_path = config.MEMORY_CARDS_DIR / "_superseded" / f"{c.id}.md"
        assert sup_path.exists()
        # Backdate the file's mtime to 35 days ago
        old_ts = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=35)
        ).timestamp()
        import os
        os.utime(sup_path, (old_ts, old_ts))

        counts = memory_prune.run_once()
        assert counts["superseded_drops"] == 1
        assert not sup_path.exists()

    def test_recent_superseded_kept(self, isolated_home):
        c = _make(
            content="x", durability=0.5,
            when=_dt.datetime.now(_dt.timezone.utc),
        )
        memory_cards.supersede(c.id)
        # Don't backdate — it's "now"
        counts = memory_prune.run_once()
        assert counts["superseded_drops"] == 0
        sup_path = config.MEMORY_CARDS_DIR / "_superseded" / f"{c.id}.md"
        assert sup_path.exists()


# ---------- Reconcile after pruning ----------


class TestReconcileAfter:
    def test_index_updated_after_drops(self, isolated_home):
        old_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=22)
        c = _make(content="x", durability=0.2, when=old_dt)
        memory_index.reconcile()
        assert memory_index.lookup_by_id(c.id) is not None

        memory_prune.run_once()
        # After prune, card is superseded → index lookup returns None
        assert memory_index.lookup_by_id(c.id) is None


# ---------- Empty / no-op ----------


class TestEmptyPasses:
    def test_run_once_with_no_cards(self, isolated_home):
        counts = memory_prune.run_once()
        assert counts["removed"] == 0

    def test_run_once_with_only_protected(self, isolated_home):
        old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=500)
        _make(content="x", durability=0.9, when=old)
        memory_index.reconcile()
        counts = memory_prune.run_once()
        assert counts["removed"] == 0

    def test_idempotent(self, isolated_home):
        old_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=22)
        _make(content="a", durability=0.2, when=old_dt)
        _make(content="b", durability=0.5, when=old_dt)
        memory_index.reconcile()

        first = memory_prune.run_once()
        second = memory_prune.run_once()
        third = memory_prune.run_once()
        # First pass drops the durability=0.2 card; second/third are no-op
        assert first["removed"] >= 1
        assert second["active_drops"] == 0
        assert third["active_drops"] == 0
