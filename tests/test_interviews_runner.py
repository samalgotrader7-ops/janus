"""Tests for v1.19.0 Phase 3 — one-shot interview runner.

Covers: end-to-end answer flow (answer → card written → state marked),
skip flow, later flow, cards_written shape, completion_pct populated,
runner respects category_filter, cards have correct provenance.
"""

from __future__ import annotations
import datetime as _dt
from unittest.mock import patch

import pytest

from janus import (
    config,
    interviews,
    interview_runner,
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
    return home


def _q(qid: str, mode: str = "text", recheck: int | None = None,
       choices: list[str] | None = None) -> interviews.Question:
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
            name=category,
            description=f"{category} desc",
            version=1,
            questions=list(qs),
        ),
    }


def _scripted_ask(answers: dict[str, str]):
    """Build a callback that returns answers[fqid] when asked.

    Missing fqid → returns interview_runner.SKIP_TOKEN.
    The "__cancel__" sentinel value triggers LATER_TOKEN.
    """
    def callback(question, fqid):
        if fqid not in answers:
            return interview_runner.SKIP_TOKEN
        v = answers[fqid]
        if v == "__cancel__":
            return interview_runner.LATER_TOKEN
        return v
    return callback


# ---------- Happy path ----------


class TestHappyPath:
    def test_answers_become_cards(self, isolated_home):
        lib = _lib("identity", _q("name"), _q("role"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({
            "identity.name": "Sam",
            "identity.role": "Network engineer",
        })
        result = interview_runner.run_one_shot(state, lib, ask)
        assert result.completed is True
        assert result.cancelled is False
        assert result.answered == 2
        assert result.skipped == 0
        assert len(result.cards_written) == 2
        # Cards exist on disk
        for card_id in result.cards_written:
            assert memory_cards.card_path(card_id).exists()

    def test_answers_have_correct_card_fields(self, isolated_home):
        lib = _lib("identity", _q("name"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({"identity.name": "Sam"})
        result = interview_runner.run_one_shot(
            state, lib, ask, scope="global",
            conversation_id="conv-test", turn=7,
        )
        card_id = result.cards_written[0]
        card = memory_cards.read_card(memory_cards.card_path(card_id))
        assert card.type == "identity"
        assert card.subject == "name"
        assert card.content == "Sam"
        assert card.confidence == 0.9  # interview answers are high-confidence
        assert card.scope == "global"
        assert card.source.origin_kind == "user_turn"
        assert card.source.conversation_id == "conv-test"
        assert card.source.turn == 7

    def test_state_marks_answered(self, isolated_home):
        lib = _lib("identity", _q("name"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({"identity.name": "Sam"})
        interview_runner.run_one_shot(state, lib, ask)
        assert "identity.name" in state.answered
        assert state.answered["identity.name"]["card_id"]

    def test_completion_pct_populated(self, isolated_home):
        lib = _lib("identity", _q("name", recheck=None), _q("role", recheck=None))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({
            "identity.name": "Sam",
            "identity.role": "engineer",
        })
        result = interview_runner.run_one_shot(state, lib, ask)
        # Both questions answered → 100% identity completion
        assert result.completion_pct["identity"] == 1.0

    def test_state_persisted_after_run(self, isolated_home):
        lib = _lib("identity", _q("name"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({"identity.name": "Sam"})
        interview_runner.run_one_shot(state, lib, ask)
        # Reload state from disk — must be persisted
        loaded = interviews.load_state("cli", "x")
        assert "identity.name" in loaded.answered


# ---------- Skip flow ----------


class TestSkip:
    def test_skip_marks_state_no_card(self, isolated_home):
        lib = _lib("identity", _q("name"), _q("role"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        # Skip name, answer role
        ask = _scripted_ask({"identity.role": "engineer"})
        result = interview_runner.run_one_shot(state, lib, ask)
        assert result.answered == 1
        assert result.skipped == 1
        assert "identity.name" in state.skipped
        assert "identity.name" not in state.answered

    def test_empty_string_treated_as_skip(self, isolated_home):
        lib = _lib("identity", _q("name"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({"identity.name": ""})
        result = interview_runner.run_one_shot(state, lib, ask)
        assert result.skipped == 1
        assert result.answered == 0

    def test_whitespace_only_treated_as_skip(self, isolated_home):
        lib = _lib("identity", _q("name"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({"identity.name": "    "})
        result = interview_runner.run_one_shot(state, lib, ask)
        assert result.skipped == 1


# ---------- Later flow (cancellation) ----------


class TestCancel:
    def test_later_stops_loop_with_cancelled_true(self, isolated_home):
        lib = _lib("identity", _q("name"), _q("role"), _q("timezone"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        # Answer name, then cancel before role
        ask = _scripted_ask({
            "identity.name": "Sam",
            "identity.role": "__cancel__",
        })
        result = interview_runner.run_one_shot(state, lib, ask)
        assert result.cancelled is True
        assert result.completed is False
        assert result.answered == 1
        # role and timezone never even got asked
        assert "identity.role" not in state.skipped

    def test_callback_exception_treated_as_cancel(self, isolated_home):
        lib = _lib("identity", _q("name"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")

        def bad_ask(q, fqid):
            raise RuntimeError("UI crashed")

        result = interview_runner.run_one_shot(state, lib, bad_ask)
        assert result.cancelled is True


# ---------- Smart-skip ----------


class TestSmartSkip:
    def test_already_known_card_skipped(self, isolated_home):
        # User already has a card for (preference, style) from organic
        # conversation. Interview must skip the matching question.
        c = memory_cards.make_card(
            type="preference", subject="style", content="terse",
            confidence=0.9, importance=0.7, durability=0.8,
            scope="global",
        )
        memory_cards.write_card(c)
        memory_index.reconcile()

        lib = _lib("preference", _q("style"), _q("emoji"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        # Only emoji should be asked
        ask = _scripted_ask({"preference.emoji": "Sparingly for tone"})
        result = interview_runner.run_one_shot(state, lib, ask)
        assert result.answered == 1
        # 'style' was never asked → not in answered or skipped
        assert "preference.style" not in state.answered
        assert "preference.style" not in state.skipped


# ---------- category_filter ----------


class TestCategoryFilter:
    def test_filter_restricts_to_one_category(self, isolated_home):
        lib = {
            "identity": interviews.Category(
                name="identity", description="x", version=1,
                questions=[_q("name")],
            ),
            "preference": interviews.Category(
                name="preference", description="x", version=1,
                questions=[_q("style")],
            ),
        }
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({
            "identity.name": "Sam",
            "preference.style": "terse",
        })
        # Run with filter — only preference questions asked
        result = interview_runner.run_one_shot(
            state, lib, ask, category_filter="preference",
        )
        assert result.answered == 1
        assert "preference.style" in state.answered
        assert "identity.name" not in state.answered


# ---------- Choices mode ----------


class TestChoicesMode:
    def test_choice_text_lands_as_card_content(self, isolated_home):
        lib = _lib(
            "preference",
            _q("style", mode="choices",
              choices=["Terse", "Verbose", "Casual"]),
        )
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({"preference.style": "Terse"})
        result = interview_runner.run_one_shot(state, lib, ask)
        card = memory_cards.read_card(
            memory_cards.card_path(result.cards_written[0])
        )
        assert card.content == "Terse"


# ---------- Render completion meter ----------


class TestCompletionMeterRender:
    def test_returns_eight_lines(self):
        pcts = {cat: 0.5 for cat in interviews.SUPPORTED_CATEGORIES}
        lines = interview_runner.render_completion_meter(pcts)
        assert len(lines) == 8

    def test_includes_percent(self):
        pcts = {"identity": 0.8}
        lines = interview_runner.render_completion_meter(pcts)
        identity_line = next(line for line in lines if "identity" in line)
        assert "80%" in identity_line

    def test_zero_for_missing_category(self):
        lines = interview_runner.render_completion_meter({})
        for line in lines:
            assert "0%" in line


# ---------- max_questions safety ----------


class TestMaxQuestions:
    def test_honors_max_questions_budget(self, isolated_home):
        # 5 questions, but max_questions=2 → only 2 asked.
        lib = _lib("identity", _q("a"), _q("b"), _q("c"), _q("d"), _q("e"))
        state = interviews.InterviewState(gateway="cli", chat_id="x")
        ask = _scripted_ask({
            "identity.a": "1", "identity.b": "2",
            "identity.c": "3", "identity.d": "4", "identity.e": "5",
        })
        result = interview_runner.run_one_shot(
            state, lib, ask, max_questions=2,
        )
        assert result.answered == 2
        assert result.completed is False
