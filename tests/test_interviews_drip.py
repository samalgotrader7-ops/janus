"""Tests for v1.19.0 Phase 4 — drip mode helpers.

Covers: consume_pending_drip_answer (apply card, skip token, cancel
token, no-pending), get_drip_question (quota gate, auto-pause at 90%,
midnight reset, no-eligible auto-pause).
"""

from __future__ import annotations
import datetime as _dt

import pytest

from janus import config, interviews, memory_cards, memory_index


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
    return home


def _q(qid: str, mode: str = "text",
       choices: list[str] | None = None,
       recheck: int | None = None) -> interviews.Question:
    return interviews.Question(
        id=qid,
        question=f"Q for {qid}?",
        mode=mode,
        choices=choices or [],
        importance=0.7,
        durability=0.8,
        recheck_days=recheck,
    )


def _lib(category: str, *qs) -> dict[str, interviews.Category]:
    return {
        category: interviews.Category(
            name=category, description=f"{category} desc", version=1,
            questions=list(qs),
        ),
    }


# ---------- consume_pending_drip_answer ----------


class TestConsumeAnswer:
    def test_no_pending_returns_not_handled(self, isolated_home):
        # Drip not active → don't intercept anything.
        handled, ack = interviews.consume_pending_drip_answer(
            "cli", "default", "anything",
        )
        assert handled is False
        assert ack == ""

    def test_drip_active_no_current_question_not_handled(self, isolated_home):
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            current_question_id="",
        )
        interviews.save_state(s)
        handled, ack = interviews.consume_pending_drip_answer(
            "cli", "default", "anything",
        )
        assert handled is False

    def test_substantive_answer_writes_card(self, isolated_home):
        # Set up: drip active, pending question
        lib = _lib("identity", _q("name"))
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            current_question_id="identity.name",
            drip_quota_remaining=1,
        )
        interviews.save_state(s)

        handled, ack = interviews.consume_pending_drip_answer(
            "cli", "default", "Sam", library=lib,
        )
        assert handled is True
        assert "identity/name" in ack

        # Card was written
        memory_index.reconcile()
        rows = memory_index.lookup_by_subject("identity", "name")
        assert len(rows) == 1
        # State updated
        loaded = interviews.load_state("cli", "default")
        assert "identity.name" in loaded.answered
        assert loaded.current_question_id == ""

    def test_skip_token_marks_skipped(self, isolated_home):
        lib = _lib("identity", _q("name"))
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            current_question_id="identity.name",
        )
        interviews.save_state(s)

        handled, ack = interviews.consume_pending_drip_answer(
            "cli", "default", "skip", library=lib,
        )
        assert handled is True
        assert "skipped" in ack.lower()
        loaded = interviews.load_state("cli", "default")
        assert "identity.name" in loaded.skipped
        assert "identity.name" not in loaded.answered
        assert loaded.current_question_id == ""

    def test_stop_drip_pauses_mode(self, isolated_home):
        lib = _lib("identity", _q("name"))
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            current_question_id="identity.name",
        )
        interviews.save_state(s)

        handled, ack = interviews.consume_pending_drip_answer(
            "cli", "default", "stop drip", library=lib,
        )
        assert handled is True
        loaded = interviews.load_state("cli", "default")
        assert loaded.mode == "idle"
        assert loaded.current_question_id == ""

    def test_choice_number_maps_to_choice_text(self, isolated_home):
        lib = _lib(
            "preference",
            _q("style", mode="choices", choices=["Terse", "Verbose", "Casual"]),
        )
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            current_question_id="preference.style",
        )
        interviews.save_state(s)

        handled, ack = interviews.consume_pending_drip_answer(
            "cli", "default", "2", library=lib,
        )
        assert handled is True
        memory_index.reconcile()
        rows = memory_index.lookup_by_subject("preference", "style")
        from pathlib import Path
        card = memory_cards.read_card(Path(rows[0]["path"]))
        assert card.content == "Verbose"


# ---------- get_drip_question ----------


class TestGetDripQuestion:
    def test_returns_none_when_not_drip(self, isolated_home):
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="idle",
        )
        interviews.save_state(s)
        result = interviews.get_drip_question("cli", "default")
        assert result is None

    def test_returns_question_when_quota_ok(self, isolated_home):
        lib = _lib("identity", _q("name"))
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            drip_quota_remaining=2,
            drip_quota_resets_at=interviews._now_iso(
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=12),
            ),
        )
        interviews.save_state(s)

        result = interviews.get_drip_question(
            "cli", "default", library=lib,
        )
        assert result is not None
        question_text, fqid = result
        assert fqid == "identity.name"
        # Quota decremented + current_question_id stamped
        loaded = interviews.load_state("cli", "default")
        assert loaded.drip_quota_remaining == 1
        assert loaded.current_question_id == "identity.name"

    def test_quota_zero_returns_none(self, isolated_home):
        lib = _lib("identity", _q("name"))
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            drip_quota_remaining=0,
            drip_quota_resets_at=interviews._now_iso(
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=12),
            ),
        )
        interviews.save_state(s)

        result = interviews.get_drip_question(
            "cli", "default", library=lib,
        )
        assert result is None

    def test_expired_window_resets_quota(self, isolated_home):
        lib = _lib("identity", _q("name"))
        # Set reset time in the past → expired
        past = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            drip_quota_remaining=0,
            drip_quota_resets_at=past,
        )
        interviews.save_state(s)

        result = interviews.get_drip_question(
            "cli", "default", library=lib,
            per_day_default=3,
        )
        assert result is not None
        # Quota was reset to 3 (default), then decremented for this question
        loaded = interviews.load_state("cli", "default")
        assert loaded.drip_quota_remaining == 2

    def test_auto_pauses_at_high_completion(self, isolated_home, monkeypatch):
        # Threshold default is 0.9 — set library to 1 question and answer it
        # → 100% completion → next get_drip_question returns None and
        # flips mode to idle.
        lib = _lib("identity", _q("name", recheck=None))
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            drip_quota_remaining=2,
            drip_quota_resets_at=interviews._now_iso(
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=12),
            ),
        )
        # Pre-mark answered → 100% completion
        interviews.mark_answered(s, "identity.name")
        interviews.save_state(s)

        result = interviews.get_drip_question(
            "cli", "default", library=lib,
        )
        assert result is None
        loaded = interviews.load_state("cli", "default")
        assert loaded.mode == "idle"

    def test_auto_pauses_when_no_eligible(self, isolated_home):
        # Library has questions but they're all answered (and recheck_days
        # is None). next_question returns None → drip auto-pauses.
        lib = _lib("identity", _q("name", recheck=None))
        s = interviews.InterviewState(
            gateway="cli", chat_id="default", mode="drip",
            drip_quota_remaining=2,
            drip_quota_resets_at=interviews._now_iso(
                _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=12),
            ),
        )
        interviews.mark_answered(s, "identity.name")
        interviews.save_state(s)

        result = interviews.get_drip_question(
            "cli", "default", library=lib,
        )
        assert result is None
        loaded = interviews.load_state("cli", "default")
        assert loaded.mode == "idle"


# ---------- render_drip_question ----------


class TestRenderDripQuestion:
    def test_includes_question_text(self):
        rendered = interviews.render_drip_question("What's your role?")
        assert "What's your role?" in rendered

    def test_includes_skip_hint(self):
        rendered = interviews.render_drip_question("anything")
        assert "skip" in rendered.lower()

    def test_includes_stop_hint(self):
        rendered = interviews.render_drip_question("anything")
        assert "stop" in rendered.lower() or "pause" in rendered.lower()
