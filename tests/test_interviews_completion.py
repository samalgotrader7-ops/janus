"""Tests for v1.19.0 Phase 6 — completion meter + /memory about-me."""

from __future__ import annotations
from pathlib import Path

import pytest

from janus import (
    config,
    interview_runner,
    interviews,
    memory_cards,
    memory_index,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    monkeypatch.setattr(
        config, "INTERVIEWS_DIR", home / "interviews", raising=False,
    )
    interviews.maybe_install_bundled()
    return home


# ---------- Completion meter rendering ----------


class TestMeterRender:
    def test_empty_state_renders_zero_for_each_category(self, isolated_home):
        state = interviews.load_state("cli", "default")
        library = interviews.load_all()
        pcts = interviews.compute_completion(state, library)
        # All 8 categories present; all 0%
        for cat in interviews.SUPPORTED_CATEGORIES:
            assert pcts[cat] == 0.0
        # Render produces 8 lines
        lines = interview_runner.render_completion_meter(pcts)
        assert len(lines) == 8
        for line in lines:
            assert "0%" in line

    def test_partial_completion_reflected(self, isolated_home):
        state = interviews.load_state("cli", "default")
        # Mark identity.name + identity.role answered (2 of 5)
        interviews.mark_answered(state, "identity.name")
        interviews.mark_answered(state, "identity.role")
        library = interviews.load_all()
        pcts = interviews.compute_completion(state, library)
        # 2 of 5 = 40%
        assert abs(pcts["identity"] - 0.4) < 0.01

    def test_overall_averages_categories(self, isolated_home):
        state = interviews.load_state("cli", "default")
        # Answer all of identity (5/5), nothing else
        for qid in ("name", "role", "timezone",
                    "years_experience", "background"):
            interviews.mark_answered(state, f"identity.{qid}")
        library = interviews.load_all()
        overall = interviews.overall_completion(state, library)
        # 1.0 across identity, 0 elsewhere → average is 1/8
        assert abs(overall - 0.125) < 0.01


# ---------- /memory about-me dispatcher (cli) ----------


class TestAboutMeDispatcher:
    def test_no_cards_prints_empty_message(self, isolated_home, capsys):
        from janus.cli import _cmd_memory_about_me
        _cmd_memory_about_me()
        out = capsys.readouterr().out
        assert "nothing yet" in out.lower() or "no cards yet" in out.lower()

    def test_cards_grouped_by_category(self, isolated_home, capsys):
        c1 = memory_cards.make_card(
            type="identity", subject="name", content="Sam",
            confidence=0.9, importance=0.9, durability=0.9,
            scope="global",
        )
        c2 = memory_cards.make_card(
            type="preference", subject="style", content="Terse",
            confidence=0.9, importance=0.7, durability=0.8,
            scope="global",
        )
        memory_cards.write_card(c1)
        memory_cards.write_card(c2)
        memory_index.reconcile()

        from janus.cli import _cmd_memory_about_me
        _cmd_memory_about_me()
        out = capsys.readouterr().out
        # Both categories present with their content
        assert "identity" in out
        assert "name" in out and "Sam" in out
        assert "preference" in out
        assert "style" in out and "Terse" in out

    def test_invitation_to_correct_at_end(self, isolated_home, capsys):
        c = memory_cards.make_card(
            type="identity", subject="name", content="Sam",
            confidence=0.9, importance=0.9, durability=0.9,
            scope="global",
        )
        memory_cards.write_card(c)
        memory_index.reconcile()

        from janus.cli import _cmd_memory_about_me
        _cmd_memory_about_me()
        out = capsys.readouterr().out
        assert "wrong" in out.lower() or "correct" in out.lower()


# ---------- Stale answers don't count toward completion ----------


class TestStaleAnswers:
    def test_stale_answer_not_counted(self, isolated_home):
        # Answer 100 days ago a question with recheck=90 → stale
        import datetime as _dt
        long_ago = (_dt.datetime.now(_dt.timezone.utc)
                    - _dt.timedelta(days=100))
        state = interviews.load_state("cli", "default")
        # goal.current_quarter has recheck_days=90 in the bundled lib
        interviews.mark_answered(state, "goal.current_quarter", when=long_ago)
        library = interviews.load_all()
        pcts = interviews.compute_completion(state, library)
        # 0 of 5 fresh → 0%
        assert pcts["goal"] == 0.0
