"""Tests for janus.memory_migrate (v1.18.0 Phase 9 bootstrap).

Covers: legacy .md → cards migration, idempotency via marker file,
section parsing, category → type mapping.
"""

from __future__ import annotations
from pathlib import Path

import pytest

from janus import config, memory, memory_cards, memory_index, memory_migrate


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    yield home


def _seed_legacy(category: str, content: str) -> None:
    """Write content to ~/.janus/memory/<category>.md."""
    p = memory.category_path(category)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------- Idempotency ----------


class TestIdempotency:
    def test_marker_initially_absent(self, isolated_home):
        assert not memory_migrate.is_done()

    def test_run_once_writes_marker(self, isolated_home):
        memory_migrate.run_once()
        assert memory_migrate.is_done()

    def test_maybe_migrate_skips_when_done(self, isolated_home):
        # First call — actually runs.
        _seed_legacy("user", "## Identity\n\nSam, solo dev.\n")
        result1 = memory_migrate.maybe_migrate()
        assert result1.get("skipped") is not True
        assert result1["migrated"] >= 1

        # Second call — skipped.
        result2 = memory_migrate.maybe_migrate()
        assert result2.get("skipped") is True
        assert result2["migrated"] == 0

    def test_reset_re_enables_migration(self, isolated_home):
        _seed_legacy("user", "## Identity\n\nSam.\n")
        memory_migrate.maybe_migrate()
        assert memory_migrate.is_done()

        memory_migrate.reset()
        assert not memory_migrate.is_done()


# ---------- Section parsing → cards ----------


class TestSectionParsing:
    def test_user_md_identity_section(self, isolated_home):
        _seed_legacy(
            "user",
            "## Identity\n\nSam, solo developer of Janus.\n",
        )
        memory_migrate.run_once()
        memory_index.reconcile()

        rows = memory_index.list_all(type="identity")
        assert len(rows) == 1
        assert rows[0]["subject"] == "identity"

    def test_multiple_sections_become_multiple_cards(self, isolated_home):
        _seed_legacy(
            "preferences",
            "## Communication\n\nTerse, code-first.\n\n"
            "## Output Style\n\nMarkdown, no emojis.\n",
        )
        memory_migrate.run_once()
        memory_index.reconcile()

        rows = memory_index.list_all(type="preference")
        assert len(rows) == 2
        subjects = {r["subject"] for r in rows}
        assert "communication" in subjects
        assert "output_style" in subjects

    def test_empty_sections_skipped(self, isolated_home):
        _seed_legacy(
            "user",
            "## Identity\n\nSam.\n\n## EmptySection\n\n\n",
        )
        result = memory_migrate.run_once()
        memory_index.reconcile()

        rows = memory_index.list_all(type="identity")
        assert len(rows) == 1  # only Identity
        assert result["skipped_empty"] >= 1

    def test_subject_slugified(self, isolated_home):
        _seed_legacy(
            "preferences",
            "## My  Big   Section Name\n\ncontent\n",
        )
        memory_migrate.run_once()
        memory_index.reconcile()

        rows = memory_index.list_all(type="preference")
        assert len(rows) == 1
        assert rows[0]["subject"] == "my_big_section_name"


# ---------- Category → type mapping ----------


class TestCategoryMapping:
    def test_soul_to_identity(self, isolated_home):
        _seed_legacy("soul", "## Persona\n\nJanus assistant.\n")
        memory_migrate.run_once()
        memory_index.reconcile()
        rows = memory_index.list_all(type="identity")
        assert any(r["subject"] == "persona" for r in rows)

    def test_project_to_project(self, isolated_home):
        _seed_legacy("project", "## Current\n\nv1.18 build.\n")
        memory_migrate.run_once()
        memory_index.reconcile()
        rows = memory_index.list_all(type="project")
        assert any(r["subject"] == "current" for r in rows)

    def test_relationships_to_relationship(self, isolated_home):
        _seed_legacy(
            "relationships",
            "## Collaborator\n\nClaude reviews PRs.\n",
        )
        memory_migrate.run_once()
        memory_index.reconcile()
        rows = memory_index.list_all(type="relationship")
        assert any(r["subject"] == "collaborator" for r in rows)

    def test_preferences_to_preference(self, isolated_home):
        _seed_legacy("preferences", "## Style\n\nTerse.\n")
        memory_migrate.run_once()
        memory_index.reconcile()
        rows = memory_index.list_all(type="preference")
        assert any(r["subject"] == "style" for r in rows)


# ---------- Provenance ----------


class TestProvenance:
    def test_migrated_cards_have_legacy_origin(self, isolated_home):
        _seed_legacy("user", "## Identity\n\nSam.\n")
        memory_migrate.run_once()
        # Read the card file and check origin_kind.
        cards_dir = config.MEMORY_CARDS_DIR
        files = list(cards_dir.glob("*.md"))
        assert len(files) == 1
        card = memory_cards.read_card(files[0])
        assert card.source.origin_kind == "legacy_migration"


# ---------- No-op when no legacy files ----------


class TestNoLegacyFiles:
    def test_no_legacy_files_zero_migrated(self, isolated_home):
        result = memory_migrate.run_once()
        assert result["migrated"] == 0
        # Marker still written so we don't re-attempt
        assert memory_migrate.is_done()

    def test_legacy_file_present_but_empty(self, isolated_home):
        _seed_legacy("user", "")
        result = memory_migrate.run_once()
        assert result["migrated"] == 0


# ---------- Full pipeline through cache.snapshot ----------


class TestCacheBootstrap:
    def test_cache_snapshot_triggers_migration(self, isolated_home):
        from janus import cache
        _seed_legacy("user", "## Identity\n\nSam.\n")
        # Migration not done yet
        assert not memory_migrate.is_done()
        # Calling snapshot should trigger maybe_migrate
        cache.snapshot()
        assert memory_migrate.is_done()
        memory_index.reconcile()
        rows = memory_index.list_all()
        assert len(rows) >= 1

    def test_cache_snapshot_idempotent(self, isolated_home):
        from janus import cache
        _seed_legacy("user", "## Identity\n\nSam.\n")
        cache.snapshot()
        cards_after_first = list(config.MEMORY_CARDS_DIR.glob("*.md"))
        cache.snapshot()
        cards_after_second = list(config.MEMORY_CARDS_DIR.glob("*.md"))
        # Same files → no duplicate writes
        assert cards_after_first == cards_after_second
