"""Tests for v1.19.0 Phase 5 — interview_ask model-callable tool."""

from __future__ import annotations

import pytest

from janus import config, interviews, memory_cards, memory_index
from janus.tools.interview_ask import InterviewAsk, UNAVAILABLE


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
    # Auto-install bundled library so the tool has questions to pick.
    interviews.maybe_install_bundled()
    return home


def _approve(*args, **kwargs):
    return True


# ---------- Surface ----------


class TestSurface:
    def test_metadata(self):
        t = InterviewAsk()
        assert t.name == "interview_ask"
        assert t.risk == "read"
        assert t.dangerous is False
        assert "category" in t.parameters["properties"]
        assert t.parameters["required"] == ["category"]


# ---------- Validation ----------


class TestValidation:
    def test_invalid_category_returns_error(self, isolated_home):
        t = InterviewAsk()
        out = t.run({"category": "not_a_real_category"}, _approve)
        assert "error" in out.lower()
        assert "invalid category" in out.lower()

    def test_missing_category_returns_error(self, isolated_home):
        t = InterviewAsk()
        out = t.run({}, _approve)
        assert "error" in out.lower()


# ---------- Callback unavailable ----------


class TestNoCallback:
    def test_no_callback_returns_unavailable_sentinel(self, isolated_home):
        t = InterviewAsk()  # no clarify_callback
        out = t.run({"category": "identity"}, _approve)
        assert out == UNAVAILABLE


# ---------- Happy path ----------


class TestHappyPath:
    def test_asks_first_eligible_and_writes_card(self, isolated_home):
        captured = {}

        def fake_clarify(question: str, choices):
            captured["question"] = question
            captured["choices"] = choices
            return "Sam"

        t = InterviewAsk(
            clarify_callback=fake_clarify,
            gateway="cli", chat_id="default",
        )
        out = t.run({"category": "identity"}, _approve)
        assert "asked identity" in out
        # First eligible bundled identity question is "name"
        assert "name" in captured["question"].lower()
        # Card landed
        memory_index.reconcile()
        rows = memory_index.lookup_by_subject("identity", "name")
        assert len(rows) == 1

    def test_explicit_question_id(self, isolated_home):
        captured = {}

        def fake_clarify(question, choices):
            captured["question"] = question
            return "Network engineer"

        t = InterviewAsk(clarify_callback=fake_clarify, gateway="cli")
        out = t.run(
            {"category": "identity", "question_id": "role"},
            _approve,
        )
        assert "asked identity.role" in out
        assert "primary role" in captured["question"].lower()

    def test_choices_question_passes_choices_to_callback(self, isolated_home):
        captured = {}

        def fake_clarify(question, choices):
            captured["question"] = question
            captured["choices"] = choices
            return "PT"

        t = InterviewAsk(clarify_callback=fake_clarify)
        # timezone is mode=choices
        out = t.run(
            {"category": "identity", "question_id": "timezone"},
            _approve,
        )
        assert "asked identity.timezone" in out
        assert captured["choices"]
        assert "PT" in captured["choices"]


# ---------- Smart-skip ----------


class TestSmartSkip:
    def test_already_covered_question_skipped(self, isolated_home):
        # Pre-existing card for (identity, name)
        c = memory_cards.make_card(
            type="identity", subject="name", content="Sam",
            confidence=0.95, importance=0.9, durability=0.95,
            scope="global",
        )
        memory_cards.write_card(c)
        memory_index.reconcile()

        called = {"n": 0}
        def fake_clarify(question, choices):
            called["n"] += 1
            return "irrelevant"

        t = InterviewAsk(clarify_callback=fake_clarify)
        # Without question_id → next_question skips identity.name and
        # asks identity.role (next eligible).
        out = t.run({"category": "identity"}, _approve)
        # The callback got called for role, NOT name
        assert "asked identity" in out
        assert called["n"] == 1

    def test_explicit_question_already_covered_returns_skipped(self, isolated_home):
        c = memory_cards.make_card(
            type="identity", subject="name", content="Sam",
            confidence=0.95, importance=0.9, durability=0.95,
            scope="global",
        )
        memory_cards.write_card(c)
        memory_index.reconcile()

        called = {"n": 0}
        def fake_clarify(q, c):
            called["n"] += 1
            return "X"

        t = InterviewAsk(clarify_callback=fake_clarify)
        out = t.run(
            {"category": "identity", "question_id": "name"},
            _approve,
        )
        assert "skipped" in out.lower() or "covered" in out.lower()
        assert called["n"] == 0  # callback never invoked


# ---------- User declines ----------


class TestUserDeclines:
    def test_empty_answer_marked_skipped(self, isolated_home):
        def fake_clarify(question, choices):
            return ""

        t = InterviewAsk(clarify_callback=fake_clarify)
        out = t.run({"category": "identity"}, _approve)
        assert "declined" in out.lower()
        # State has the skip recorded
        state = interviews.load_state("cli", "default")
        assert any("identity." in k for k in state.skipped)

    def test_none_answer_marked_skipped(self, isolated_home):
        def fake_clarify(question, choices):
            return None

        t = InterviewAsk(clarify_callback=fake_clarify)
        out = t.run({"category": "identity"}, _approve)
        assert "declined" in out.lower()


# ---------- Registration ----------


class TestRegistration:
    def test_in_default_registry(self):
        from janus.tools import default_registry
        reg = default_registry()
        names = [s["function"]["name"] for s in reg.schemas()]
        assert "interview_ask" in names


# ---------- Numeric choice mapping ----------


class TestChoiceMapping:
    def test_numeric_answer_maps_to_choice_text(self, isolated_home):
        def fake_clarify(question, choices):
            return "1"  # picking first choice numerically

        t = InterviewAsk(clarify_callback=fake_clarify)
        out = t.run(
            {"category": "identity", "question_id": "timezone"},
            _approve,
        )
        assert "asked identity.timezone" in out
        # Card content is the choice text, not "1"
        memory_index.reconcile()
        rows = memory_index.lookup_by_subject("identity", "timezone")
        assert len(rows) == 1
        from pathlib import Path
        card = memory_cards.read_card(Path(rows[0]["path"]))
        assert card.content == "PT"  # first choice in identity.timezone
