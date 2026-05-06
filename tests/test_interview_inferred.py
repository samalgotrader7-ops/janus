"""Tests for v1.19.0 Phase 7 — inferred-suggestion heuristic."""

from __future__ import annotations
import datetime as _dt
import json

import pytest

from janus import (
    config,
    interview_inferred,
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
    return home


# ---------- scan() — keyword detection ----------


class TestScan:
    def test_no_match_empty_list(self):
        assert interview_inferred.scan("hello world") == []

    def test_project_keyword(self):
        hints = interview_inferred.scan(
            "I'm working on a forex bot in my spare time"
        )
        assert any(h.category == "project" for h in hints)

    def test_identity_keyword(self):
        hints = interview_inferred.scan("Hi, I'm a network engineer")
        assert any(h.category == "identity" for h in hints)

    def test_habit_keyword(self):
        hints = interview_inferred.scan(
            "every morning I review yesterday's positions"
        )
        assert any(h.category == "habit" for h in hints)

    def test_relationship_keyword(self):
        hints = interview_inferred.scan(
            "my team is remote and we sync weekly"
        )
        assert any(h.category == "relationship" for h in hints)

    def test_excluded_categories_skipped(self):
        # Even though "I'm a" matches identity, exclude → no identity hint
        hints = interview_inferred.scan(
            "I'm a developer and I'm working on X",
            excluded_categories={"identity"},
        )
        cats = {h.category for h in hints}
        assert "identity" not in cats
        assert "project" in cats

    def test_one_hint_per_category(self):
        # Multiple project keywords in same text → at most ONE project hint
        hints = interview_inferred.scan(
            "I'm working on Janus, my project. Also shipping a feature."
        )
        project_hints = [h for h in hints if h.category == "project"]
        assert len(project_hints) == 1

    def test_priority_order(self):
        # Identity comes before project — when both match, identity is
        # listed first.
        hints = interview_inferred.scan(
            "I'm a developer working on a side project"
        )
        assert hints[0].category == "identity"


# ---------- covered_categories ----------


class TestCovered:
    def test_no_cards_empty(self, isolated_home):
        assert interview_inferred.covered_categories() == set()

    def test_returns_categories_with_cards(self, isolated_home):
        c = memory_cards.make_card(
            type="project", subject="x", content="some project",
            confidence=0.9, importance=0.7, durability=0.7,
            scope="global",
        )
        memory_cards.write_card(c)
        assert "project" in interview_inferred.covered_categories()


# ---------- scan_and_queue ----------


class TestScanAndQueue:
    def test_queues_when_match_and_not_covered(self, isolated_home):
        result = interview_inferred.scan_and_queue(
            "I'm working on a forex bot",
            "Got it.",
            gateway="cli", chat_id="default",
        )
        assert result is not None
        assert result.category == "project"
        # State has pending
        peek = interview_inferred.peek_pending("cli", "default")
        assert peek is not None
        assert peek.category == "project"

    def test_skips_already_covered_category(self, isolated_home):
        # User already has a project card → no offer
        c = memory_cards.make_card(
            type="project", subject="x", content="existing",
            confidence=0.9, importance=0.7, durability=0.7,
            scope="global",
        )
        memory_cards.write_card(c)

        result = interview_inferred.scan_and_queue(
            "I'm working on something new",
            "Got it.",
            gateway="cli", chat_id="default",
        )
        assert result is None

    def test_skips_categories_in_cooldown(self, isolated_home):
        # User declined project earlier → cooldown active
        interview_inferred.mark_declined("cli", "default", "project")
        result = interview_inferred.scan_and_queue(
            "I'm working on a forex bot",
            "Got it.",
            gateway="cli", chat_id="default",
        )
        assert result is None

    def test_skips_when_pending_already_exists(self, isolated_home):
        # First call queues
        interview_inferred.scan_and_queue(
            "I'm working on a forex bot",
            "ok", gateway="cli", chat_id="default",
        )
        # Second call: should NOT queue (don't pile up)
        result = interview_inferred.scan_and_queue(
            "I'm a developer",  # different category
            "ok", gateway="cli", chat_id="default",
        )
        assert result is None
        # Pending still has the original
        peek = interview_inferred.peek_pending("cli", "default")
        assert peek.category == "project"


# ---------- pop_pending / peek_pending ----------


class TestPopPeek:
    def test_pop_returns_none_when_empty(self, isolated_home):
        assert interview_inferred.pop_pending("cli", "default") is None

    def test_pop_returns_and_removes(self, isolated_home):
        interview_inferred.scan_and_queue(
            "I'm working on X", "ok",
            gateway="cli", chat_id="default",
        )
        h = interview_inferred.pop_pending("cli", "default")
        assert h is not None
        assert h.category == "project"
        # Now empty
        assert interview_inferred.pop_pending("cli", "default") is None

    def test_peek_does_not_remove(self, isolated_home):
        interview_inferred.scan_and_queue(
            "I'm working on X", "ok",
            gateway="cli", chat_id="default",
        )
        h1 = interview_inferred.peek_pending("cli", "default")
        h2 = interview_inferred.peek_pending("cli", "default")
        assert h1 is not None and h2 is not None
        assert h1.category == h2.category


# ---------- mark_declined / cooldown ----------


class TestCooldown:
    def test_decline_marks_cooldown_and_drops_pending(self, isolated_home):
        interview_inferred.scan_and_queue(
            "I'm working on X", "ok",
            gateway="cli", chat_id="default",
        )
        interview_inferred.mark_declined("cli", "default", "project")
        # Pending was cleared
        assert interview_inferred.peek_pending("cli", "default") is None
        # Re-scan still returns None (cooldown)
        result = interview_inferred.scan_and_queue(
            "I'm building a new project", "ok",
            gateway="cli", chat_id="default",
        )
        assert result is None

    def test_cooldown_expires_after_window(self, isolated_home):
        # Mark declined 31 days ago → cooldown elapsed
        long_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=31)
        interview_inferred.mark_declined(
            "cli", "default", "project", now=long_ago,
        )
        # Re-scan should queue again
        result = interview_inferred.scan_and_queue(
            "I'm working on Janus", "ok",
            gateway="cli", chat_id="default",
        )
        assert result is not None


# ---------- render_offer ----------


class TestRenderOffer:
    def test_includes_category_and_phrase(self):
        h = interview_inferred.Hint(
            category="project",
            matched_phrase="i'm working on",
        )
        rendered = interview_inferred.render_offer(h)
        assert "project" in rendered
        assert "i'm working on" in rendered

    def test_includes_response_options(self):
        h = interview_inferred.Hint(category="project", matched_phrase="x")
        rendered = interview_inferred.render_offer(h)
        assert "yes" in rendered.lower()
        assert "no" in rendered.lower()
        assert "mute" in rendered.lower()


# ---------- propose_diff hook ----------


class TestProposeDiffHook:
    def test_propose_diff_queues_inferred_offer(
        self, isolated_home, monkeypatch,
    ):
        # Stub the LLM call so propose_diff can run end-to-end
        from janus import memory
        monkeypatch.setattr(config, "MEMORY_PROPOSE_ENABLED", True)
        monkeypatch.setattr(
            memory, "_chat_with_model",
            lambda **kw: {
                "role": "assistant",
                "content": '{"ops": [], "cards": []}',
            },
        )
        # Run propose_diff with text that should trigger a project hint
        memory.propose_diff(
            "I'm working on a new project — a forex bot",
            "Got it!",
        )
        # Check that an offer was queued (origin defaults to cli/default)
        peek = interview_inferred.peek_pending("cli", "default")
        assert peek is not None
        assert peek.category == "project"
