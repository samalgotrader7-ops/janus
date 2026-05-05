"""Tests for v1.18 /memory UX expansion (Phase 7).

Covers behavior-bearing pieces:
- /memory pause writes marker; propose_diff honors it
- /memory resume removes marker
- janus memory CLI subcommands route to the right modules
- The marker file does NOT affect recall (recall stays read-only)
"""

from __future__ import annotations
import json

import pytest

from janus import config, memory, memory_cards, memory_index, memory_recall


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    monkeypatch.setattr(config, "MEMORY_PROPOSE_ENABLED", True)
    memory_recall.reset_reconcile_flag()
    yield home


# ---------- /memory pause / resume ----------


class TestPauseResume:
    def test_paused_marker_blocks_propose_diff(self, isolated_home):
        # Without marker → propose_diff hits the LLM (we'd see exception
        # because no API key in tests). With marker → returns empty fast.
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        (config.MEMORY_DIR / "_paused").touch()
        result = memory.propose_diff("hi", "hello")
        assert result == {"ops": [], "cards": []}

    def test_resume_removes_marker_effect(self, isolated_home, monkeypatch):
        # With _paused, returns empty fast.
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        (config.MEMORY_DIR / "_paused").touch()
        result = memory.propose_diff("hi", "hello")
        assert result == {"ops": [], "cards": []}

        # Remove marker → propose_diff would now call the LLM. Stub it.
        (config.MEMORY_DIR / "_paused").unlink()
        monkeypatch.setattr(
            memory,
            "_chat_with_model",
            lambda **kw: {
                "role": "assistant",
                "content": '{"ops": [], "cards": []}',
            },
        )
        result = memory.propose_diff("hi", "hello")
        # Empty (model decided no extraction) but it WAS called.
        assert result == {"ops": [], "cards": []}

    def test_pause_does_not_affect_recall(self, isolated_home):
        # Pause = write-side only. Recall (read) keeps working.
        c = memory_cards.make_card(
            type="preference", subject="x", content="findable",
            confidence=0.5, importance=0.5, durability=0.5,
            scope="global",
        )
        memory_cards.write_card(c)

        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        (config.MEMORY_DIR / "_paused").touch()

        cards = memory_recall.top_k(
            "findable", current_scope="global",
        )
        assert len(cards) == 1


# ---------- janus memory CLI dispatcher ----------


class TestCliDispatch:
    def test_stats_subcommand(self, isolated_home, capsys, monkeypatch):
        # Seed a card so stats has something to report.
        c = memory_cards.make_card(
            type="preference", subject="x", content="something",
            confidence=0.8, importance=0.5, durability=0.5,
            scope="global",
        )
        memory_cards.write_card(c)

        from janus.__main__ import _run_memory_cli
        _run_memory_cli(["stats"])
        out = capsys.readouterr().out
        assert "Memory cards: 1" in out
        assert "preference: 1" in out

    def test_pause_subcommand_writes_marker(self, isolated_home, capsys):
        from janus.__main__ import _run_memory_cli
        _run_memory_cli(["pause"])
        assert (config.MEMORY_DIR / "_paused").exists()
        assert "paused" in capsys.readouterr().out.lower()

    def test_resume_subcommand_removes_marker(self, isolated_home, capsys):
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        (config.MEMORY_DIR / "_paused").touch()

        from janus.__main__ import _run_memory_cli
        _run_memory_cli(["resume"])
        assert not (config.MEMORY_DIR / "_paused").exists()
        assert "enabled" in capsys.readouterr().out.lower()

    def test_reindex_subcommand_rebuilds_db(self, isolated_home, capsys):
        c = memory_cards.make_card(
            type="preference", subject="x", content="something",
            confidence=0.5, importance=0.5, durability=0.5,
            scope="global",
        )
        memory_cards.write_card(c)
        memory_index.reconcile()
        # Force the DB to a stale state by deleting it.
        memory_index.reset()
        assert not (config.MEMORY_DIR / "index.db").exists()

        from janus.__main__ import _run_memory_cli
        _run_memory_cli(["reindex"])
        assert (config.MEMORY_DIR / "index.db").exists()
        out = capsys.readouterr().out
        assert "added" in out
        # The single card was rebuilt
        rows = memory_index.list_all()
        assert len(rows) == 1

    def test_show_subcommand_dumps_card(self, isolated_home, capsys):
        c = memory_cards.make_card(
            type="preference", subject="show_me", content="anchor_here",
            confidence=0.5, importance=0.5, durability=0.5,
            scope="global",
        )
        memory_cards.write_card(c)

        from janus.__main__ import _run_memory_cli
        _run_memory_cli(["show", c.id])
        out = capsys.readouterr().out
        assert "anchor_here" in out
        assert "show_me" in out

    def test_show_subcommand_missing_id(self, isolated_home, capsys):
        from janus.__main__ import _run_memory_cli
        with pytest.raises(SystemExit):
            _run_memory_cli(["show", "no-such-id"])

    def test_search_subcommand_finds_card(self, isolated_home, capsys):
        c = memory_cards.make_card(
            type="preference", subject="findme",
            content="searchable anchor word",
            confidence=0.5, importance=0.5, durability=0.5,
            scope="global",
        )
        memory_cards.write_card(c)
        memory_index.reconcile()

        from janus.__main__ import _run_memory_cli
        _run_memory_cli(["search", "anchor"])
        out = capsys.readouterr().out
        assert "findme" in out

    def test_clear_subcommand_requires_type(self, isolated_home, capsys):
        from janus.__main__ import _run_memory_cli
        with pytest.raises(SystemExit):
            _run_memory_cli(["clear"])
        assert "usage" in capsys.readouterr().out.lower()

    def test_help_subcommand(self, isolated_home, capsys):
        from janus.__main__ import _run_memory_cli
        _run_memory_cli([])
        out = capsys.readouterr().out
        assert "memory" in out.lower()
        assert "stats" in out
        assert "search" in out
