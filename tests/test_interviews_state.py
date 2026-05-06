"""Tests for v1.19.0 Phase 2 — interview state machine + smart-skip."""

from __future__ import annotations
import datetime as _dt
from pathlib import Path

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


def _make_question(qid: str = "q", *, mode: str = "text",
                   recheck_days: int | None = None,
                   importance: float = 0.7,
                   durability: float = 0.7) -> interviews.Question:
    return interviews.Question(
        id=qid,
        question=f"What about {qid}?",
        mode=mode,
        importance=importance,
        durability=durability,
        recheck_days=recheck_days,
    )


def _make_library(category: str, *qids: str,
                  recheck_days: int | None = None) -> dict[str, interviews.Category]:
    cat = interviews.Category(
        name=category,
        description=f"{category} qs",
        version=1,
        questions=[_make_question(q, recheck_days=recheck_days) for q in qids],
    )
    return {category: cat}


# ---------- save / load round-trip ----------


class TestStateIO:
    def test_load_returns_blank_when_missing(self, isolated_home):
        s = interviews.load_state("cli", "default")
        assert s.gateway == "cli"
        assert s.chat_id == "default"
        assert s.mode == "idle"
        assert s.answered == {}
        assert s.skipped == {}

    def test_save_then_load_round_trip(self, isolated_home):
        s = interviews.InterviewState(
            gateway="telegram", chat_id="42", mode="drip",
            started_at="2026-05-06T20:00:00Z",
            current_category="identity",
            current_question_id="identity.name",
            answered={
                "identity.name": {
                    "answered_at": "2026-05-06T20:01:00Z",
                    "card_id": "card-123",
                },
            },
            skipped={
                "identity.years_experience": {
                    "skipped_at": "2026-05-06T20:02:00Z",
                },
            },
            drip_quota_remaining=1,
            drip_quota_resets_at="2026-05-07T00:00:00Z",
            completion_pct={"identity": 0.8},
        )
        interviews.save_state(s)
        loaded = interviews.load_state("telegram", "42")
        assert loaded.mode == "drip"
        assert loaded.answered["identity.name"]["card_id"] == "card-123"
        assert "identity.years_experience" in loaded.skipped
        assert loaded.drip_quota_remaining == 1
        assert loaded.completion_pct == {"identity": 0.8}

    def test_save_path_uses_safe_chat_id(self, isolated_home):
        s = interviews.InterviewState(
            gateway="telegram", chat_id="-1001234567890",
        )
        interviews.save_state(s)
        path = interviews.state_path("telegram", "-1001234567890")
        assert path.exists()
        # Chars stay safe-ish: '-' is allowed
        assert "-1001234567890" in path.name

    def test_save_path_sanitizes_weird_chars(self, isolated_home):
        s = interviews.InterviewState(
            gateway="telegram", chat_id="../../etc/passwd",
        )
        interviews.save_state(s)
        # Path lands inside the state dir; no traversal
        path = interviews.state_path("telegram", "../../etc/passwd")
        assert "etc/passwd" not in str(path)
        assert path.parent.name == "_state"

    def test_load_handles_corrupt_json(self, isolated_home):
        path = interviews.state_path("cli", "default")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {", encoding="utf-8")
        # Doesn't crash; returns blank state
        s = interviews.load_state("cli", "default")
        assert s.mode == "idle"


# ---------- mark_answered / mark_skipped ----------


class TestMarkers:
    def test_mark_answered_records_timestamp(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_answered(s, "identity.name", card_id="abc")
        assert "identity.name" in s.answered
        assert s.answered["identity.name"]["card_id"] == "abc"
        assert s.answered["identity.name"]["answered_at"]  # truthy

    def test_mark_answered_clears_prior_skip(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_skipped(s, "identity.role")
        assert "identity.role" in s.skipped
        interviews.mark_answered(s, "identity.role", card_id="xyz")
        assert "identity.role" in s.answered
        assert "identity.role" not in s.skipped

    def test_mark_skipped_records_timestamp(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_skipped(s, "identity.years_experience")
        assert "identity.years_experience" in s.skipped


# ---------- is_eligible: smart-skip rules ----------


class TestIsEligible:
    def test_unanswered_unskipped_eligible(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        q = _make_question("name")
        assert interviews.is_eligible(s, "identity", q, check_cards_layer=False)

    def test_answered_no_recheck_not_eligible(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_answered(s, "identity.name")
        q = _make_question("name", recheck_days=None)
        assert not interviews.is_eligible(
            s, "identity", q, check_cards_layer=False,
        )

    def test_answered_recheck_not_elapsed_not_eligible(self, isolated_home):
        # Answered yesterday, recheck=90 → still fresh
        yesterday = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_answered(s, "goal.current_quarter", when=yesterday)
        q = _make_question("current_quarter", recheck_days=90)
        assert not interviews.is_eligible(
            s, "goal", q, check_cards_layer=False,
        )

    def test_answered_recheck_elapsed_eligible(self, isolated_home):
        # Answered 100 days ago, recheck=90 → due for re-ask
        long_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=100)
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_answered(s, "goal.current_quarter", when=long_ago)
        q = _make_question("current_quarter", recheck_days=90)
        assert interviews.is_eligible(
            s, "goal", q, check_cards_layer=False,
        )

    def test_skipped_recently_not_eligible(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_skipped(s, "identity.years_experience")
        q = _make_question("years_experience")
        assert not interviews.is_eligible(
            s, "identity", q, check_cards_layer=False,
        )

    def test_skipped_after_cooldown_eligible(self, isolated_home):
        # Skipped 8 days ago, cooldown=7 → eligible again
        long_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=8)
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_skipped(s, "identity.years_experience", when=long_ago)
        q = _make_question("years_experience")
        assert interviews.is_eligible(
            s, "identity", q, check_cards_layer=False,
        )

    def test_existing_card_skips_question(self, isolated_home):
        # v1.18 cards layer already has a card for (preference, style) →
        # interview must not re-ask.
        c = memory_cards.make_card(
            type="preference",
            subject="style",
            content="terse, code-first",
            confidence=0.9, importance=0.7, durability=0.8,
            scope="global",
        )
        memory_cards.write_card(c)
        memory_index.reconcile()

        s = interviews.InterviewState(gateway="cli", chat_id="x")
        q = _make_question("style")
        assert not interviews.is_eligible(
            s, "preference", q, check_cards_layer=True,
        )

    def test_existing_card_other_subject_does_not_skip(self, isolated_home):
        c = memory_cards.make_card(
            type="preference",
            subject="emoji",
            content="never",
            confidence=0.9, importance=0.5, durability=0.8,
            scope="global",
        )
        memory_cards.write_card(c)
        memory_index.reconcile()

        s = interviews.InterviewState(gateway="cli", chat_id="x")
        # Asking about 'style' — different subject — shouldn't be blocked
        q = _make_question("style")
        assert interviews.is_eligible(
            s, "preference", q, check_cards_layer=True,
        )


# ---------- next_question ----------


class TestNextQuestion:
    def test_returns_first_eligible(self, isolated_home):
        lib = _make_library("identity", "name", "role", "timezone")
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        result = interviews.next_question(
            s, lib, check_cards_layer=False,
        )
        assert result is not None
        cat, q = result
        assert cat.name == "identity"
        assert q.id == "name"

    def test_skips_answered_walks_forward(self, isolated_home):
        lib = _make_library("identity", "name", "role", "timezone")
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_answered(s, "identity.name")
        result = interviews.next_question(
            s, lib, check_cards_layer=False,
        )
        cat, q = result
        assert q.id == "role"

    def test_returns_none_when_nothing_eligible(self, isolated_home):
        lib = _make_library("identity", "name")
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_answered(s, "identity.name")
        result = interviews.next_question(
            s, lib, check_cards_layer=False,
        )
        assert result is None

    def test_category_filter_restricts(self, isolated_home):
        lib = {
            "identity": interviews.Category(
                name="identity", description="x", version=1,
                questions=[_make_question("name")],
            ),
            "preference": interviews.Category(
                name="preference", description="x", version=1,
                questions=[_make_question("style")],
            ),
        }
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        # Filter to preference only
        result = interviews.next_question(
            s, lib, category_filter="preference",
            check_cards_layer=False,
        )
        cat, q = result
        assert cat.name == "preference"
        assert q.id == "style"

    def test_walks_categories_in_supported_order(self, isolated_home):
        # All 8 categories in the library, all answered except the LAST one
        # (relationship). next_question walks SUPPORTED_CATEGORIES in order
        # and finds relationship.
        lib: dict[str, interviews.Category] = {}
        for cat_name in interviews.SUPPORTED_CATEGORIES:
            lib[cat_name] = interviews.Category(
                name=cat_name, description="x", version=1,
                questions=[_make_question("q1")],
            )
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        # Answer everything except relationship
        for cat_name in interviews.SUPPORTED_CATEGORIES:
            if cat_name != "relationship":
                interviews.mark_answered(s, f"{cat_name}.q1")
        result = interviews.next_question(
            s, lib, check_cards_layer=False,
        )
        cat, q = result
        assert cat.name == "relationship"


# ---------- completion meter ----------


class TestCompletion:
    def test_zero_when_nothing_answered(self, isolated_home):
        lib = _make_library("identity", "name", "role")
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        # Only one category in lib; others 0
        pcts = interviews.compute_completion(s, lib)
        assert pcts["identity"] == 0.0

    def test_full_when_all_answered(self, isolated_home):
        lib = _make_library("identity", "name", "role", recheck_days=None)
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_answered(s, "identity.name")
        interviews.mark_answered(s, "identity.role")
        pcts = interviews.compute_completion(s, lib)
        assert pcts["identity"] == 1.0

    def test_partial_when_some_answered(self, isolated_home):
        lib = _make_library("identity", "name", "role", "timezone",
                             recheck_days=None)
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        interviews.mark_answered(s, "identity.name")
        pcts = interviews.compute_completion(s, lib)
        assert abs(pcts["identity"] - 1/3) < 0.01

    def test_stale_answer_not_counted(self, isolated_home):
        # Answered 100 days ago, recheck=90 → stale → doesn't count
        lib = _make_library("goal", "current", recheck_days=90)
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        long_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=100)
        interviews.mark_answered(s, "goal.current", when=long_ago)
        pcts = interviews.compute_completion(s, lib)
        assert pcts["goal"] == 0.0

    def test_overall_averages_categories(self, isolated_home):
        # Lib with all 8 cats; answer half
        lib: dict[str, interviews.Category] = {}
        for cat_name in interviews.SUPPORTED_CATEGORIES:
            lib[cat_name] = interviews.Category(
                name=cat_name, description="x", version=1,
                questions=[_make_question("q1", recheck_days=None)],
            )
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        # Answer 4 of 8 categories
        for cat_name in list(interviews.SUPPORTED_CATEGORIES)[:4]:
            interviews.mark_answered(s, f"{cat_name}.q1")
        overall = interviews.overall_completion(s, lib)
        assert overall == 0.5


# ---------- drip quota ----------


class TestDripQuota:
    def test_reset_sets_quota(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        now = _dt.datetime(2026, 5, 6, 12, 0, 0, tzinfo=_dt.timezone.utc)
        interviews.reset_drip_quota(s, per_day=2, now=now)
        assert s.drip_quota_remaining == 2
        # Resets at midnight of next day
        assert s.drip_quota_resets_at.startswith("2026-05-07T00:00:00")

    def test_quota_window_expired_when_no_reset_set(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        # No drip_quota_resets_at set → expired
        assert interviews.quota_window_expired(s)

    def test_quota_window_not_expired(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        future = (_dt.datetime.now(_dt.timezone.utc)
                  + _dt.timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        s.drip_quota_resets_at = future
        assert not interviews.quota_window_expired(s)

    def test_quota_window_expired_after_reset_time(self, isolated_home):
        s = interviews.InterviewState(gateway="cli", chat_id="x")
        past = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        s.drip_quota_resets_at = past
        assert interviews.quota_window_expired(s)
