"""Tests for janus.memory_cards (v1.18.0 Phase 1).

Phase 1 covers the data layer only — frontmatter round-trip, content-
hash IDs, atomic write, validation. No SQLite, no extraction, no recall.
"""

from __future__ import annotations
import datetime as _dt
import os
from pathlib import Path

import pytest

from janus import config, memory_cards


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect MEMORY_DIR + MEMORY_CARDS_DIR to a tmp area."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    return home


def _basic_card(**overrides) -> memory_cards.Card:
    """Helper: build a minimally valid Card with sensible defaults."""
    defaults = dict(
        type="preference",
        subject="coffee",
        content="black, no sugar",
        confidence=0.9,
        importance=0.6,
        durability=0.8,
        scope="global",
        source=memory_cards.Source(
            conversation_id="conv_test",
            turn=7,
            gateway="cli",
            origin_kind="user_turn",
        ),
    )
    defaults.update(overrides)
    return memory_cards.make_card(**defaults)


# ---------- Card ID derivation ----------


class TestCardId:
    def test_same_content_same_day_same_id(self):
        when = _dt.datetime(2026, 5, 5, 10, 0, 0)
        a = memory_cards.card_id("hello world", when=when)
        b = memory_cards.card_id("hello world", when=when)
        assert a == b

    def test_different_content_different_id(self):
        when = _dt.datetime(2026, 5, 5, 10, 0, 0)
        assert memory_cards.card_id("hello", when=when) != memory_cards.card_id("world", when=when)

    def test_same_content_different_day_different_id(self):
        a = memory_cards.card_id("hello", when=_dt.datetime(2026, 5, 5))
        b = memory_cards.card_id("hello", when=_dt.datetime(2026, 5, 6))
        assert a != b
        assert a.startswith("2026-05-05-")
        assert b.startswith("2026-05-06-")

    def test_id_format_is_yyyymmdd_dash_hex8(self):
        when = _dt.datetime(2026, 5, 5)
        cid = memory_cards.card_id("anything", when=when)
        # YYYY-MM-DD-XXXXXXXX (10 + 1 + 8 = 19 chars)
        assert len(cid) == 19
        assert cid.count("-") == 3
        # Last segment is 8 hex chars
        last = cid.rsplit("-", 1)[1]
        assert len(last) == 8
        assert all(c in "0123456789abcdef" for c in last)

    def test_id_stable_across_whitespace(self):
        # sha is computed over content.strip(); leading/trailing whitespace
        # MUST NOT change the id.
        when = _dt.datetime(2026, 5, 5)
        a = memory_cards.card_id("hello world", when=when)
        b = memory_cards.card_id("  hello world  ", when=when)
        assert a == b

    def test_card_path_under_cards_dir(self, isolated_home):
        cid = "2026-05-05-deadbeef"
        p = memory_cards.card_path(cid)
        assert p.parent == config.MEMORY_CARDS_DIR
        assert p.name == "2026-05-05-deadbeef.md"


# ---------- Validation ----------


class TestValidation:
    def test_valid_card_passes(self):
        memory_cards.validate(_basic_card())

    def test_invalid_type_rejected(self):
        card = _basic_card(type="not_a_real_type")
        with pytest.raises(memory_cards.CardValidationError, match="invalid type"):
            memory_cards.validate(card)

    @pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0, -1.0])
    def test_score_out_of_range_rejected(self, bad):
        with pytest.raises(memory_cards.CardValidationError):
            memory_cards.validate(_basic_card(confidence=bad))

    def test_each_score_field_validated(self):
        for field in ("confidence", "importance", "durability"):
            with pytest.raises(memory_cards.CardValidationError, match=field):
                memory_cards.validate(_basic_card(**{field: 1.5}))

    def test_empty_subject_rejected(self):
        with pytest.raises(memory_cards.CardValidationError, match="subject"):
            memory_cards.validate(_basic_card(subject=""))

    def test_whitespace_only_subject_rejected(self):
        with pytest.raises(memory_cards.CardValidationError, match="subject"):
            memory_cards.validate(_basic_card(subject="   "))

    def test_empty_content_rejected(self):
        with pytest.raises(memory_cards.CardValidationError, match="content"):
            memory_cards.validate(_basic_card(content=""))

    @pytest.mark.parametrize("scope", ["weird:scope", "ftp:foo", "telegram:", "project:", "global2"])
    def test_invalid_scope_rejected(self, scope):
        with pytest.raises(memory_cards.CardValidationError):
            memory_cards.validate(_basic_card(scope=scope))

    @pytest.mark.parametrize("scope", [
        "global",
        "cli",
        "telegram:12345",
        "telegram:-1001234567890",
        "web:abc",
        "project:/Users/sam/work",
        "project:C:/Users/Sam/Projects",
    ])
    def test_valid_scopes_accepted(self, scope):
        memory_cards.validate(_basic_card(scope=scope))

    def test_invalid_origin_kind_rejected(self):
        card = _basic_card()
        card.source.origin_kind = "not_a_kind"
        with pytest.raises(memory_cards.CardValidationError, match="origin_kind"):
            memory_cards.validate(card)

    @pytest.mark.parametrize("kind", list(memory_cards.ORIGIN_KINDS))
    def test_each_origin_kind_accepted(self, kind):
        card = _basic_card()
        card.source.origin_kind = kind
        memory_cards.validate(card)


# ---------- Render / parse round-trip ----------


class TestRoundTrip:
    def test_round_trip_basic(self):
        card = _basic_card()
        text = memory_cards.render(card)
        parsed = memory_cards.parse(text)
        assert parsed.id == card.id
        assert parsed.type == card.type
        assert parsed.subject == card.subject
        assert parsed.content == card.content
        assert parsed.confidence == card.confidence
        assert parsed.importance == card.importance
        assert parsed.durability == card.durability
        assert parsed.scope == card.scope
        assert parsed.created == card.created
        assert parsed.source.conversation_id == card.source.conversation_id
        assert parsed.source.turn == card.source.turn
        assert parsed.source.gateway == card.source.gateway
        assert parsed.source.origin_kind == card.source.origin_kind

    def test_round_trip_subject_with_colon(self):
        card = _basic_card(subject="user's preference: black")
        parsed = memory_cards.parse(memory_cards.render(card))
        assert parsed.subject == "user's preference: black"

    def test_round_trip_subject_looks_like_bool(self):
        # "true" as a subject must round-trip as the string, not bool.
        card = _basic_card(subject="true")
        parsed = memory_cards.parse(memory_cards.render(card))
        assert parsed.subject == "true"
        assert isinstance(parsed.subject, str)

    def test_round_trip_subject_looks_like_number(self):
        card = _basic_card(subject="42")
        parsed = memory_cards.parse(memory_cards.render(card))
        assert parsed.subject == "42"
        assert isinstance(parsed.subject, str)

    def test_round_trip_content_with_special_chars(self):
        card = _basic_card(content="Has #hashtag and: colon and \"quotes\".")
        parsed = memory_cards.parse(memory_cards.render(card))
        assert parsed.content == card.content

    def test_round_trip_multiline_content(self):
        # Content lives in the body, not frontmatter — multi-line is fine.
        body = "first line\nsecond line\nthird line"
        card = _basic_card(content=body)
        parsed = memory_cards.parse(memory_cards.render(card))
        assert parsed.content == body

    @pytest.mark.parametrize("scope", [
        "global", "cli", "telegram:12345", "web:s_abc",
        "project:/Users/sam/work",
    ])
    def test_round_trip_each_scope(self, scope):
        card = _basic_card(scope=scope)
        parsed = memory_cards.parse(memory_cards.render(card))
        assert parsed.scope == scope

    def test_render_includes_body_text(self):
        card = _basic_card(content="this is the body text")
        text = memory_cards.render(card)
        # body sits after the second --- closer
        body_part = text.split("---", 2)[-1]
        assert "this is the body text" in body_part

    def test_render_grep_friendly(self):
        # The whole point of having body content: grep works without yaml parser.
        card = _basic_card(content="ANCHOR_FOR_GREP_TEST")
        text = memory_cards.render(card)
        # Simulate `grep ANCHOR_FOR_GREP_TEST`: just substring check
        assert "ANCHOR_FOR_GREP_TEST" in text

    def test_render_validates_first(self):
        card = _basic_card(type="bogus")
        with pytest.raises(memory_cards.CardValidationError):
            memory_cards.render(card)

    def test_parse_missing_frontmatter(self):
        with pytest.raises(memory_cards.CardValidationError, match="frontmatter"):
            memory_cards.parse("just some text, no frontmatter")

    def test_quote_string_rejects_newline(self):
        with pytest.raises(ValueError, match="multi-line"):
            memory_cards._quote_string("line1\nline2")


# ---------- Disk I/O ----------


class TestDiskIO:
    def test_write_and_read_round_trip(self, isolated_home):
        card = _basic_card()
        path = memory_cards.write_card(card)
        assert path.exists()
        assert path.parent == config.MEMORY_CARDS_DIR
        loaded = memory_cards.read_card(path)
        assert loaded.id == card.id
        assert loaded.content == card.content
        assert loaded.type == card.type

    def test_write_creates_cards_dir(self, isolated_home):
        # cards/ dir doesn't exist initially.
        assert not config.MEMORY_CARDS_DIR.exists()
        memory_cards.write_card(_basic_card())
        assert config.MEMORY_CARDS_DIR.is_dir()

    def test_write_idempotent_for_same_content(self, isolated_home):
        when = _dt.datetime(2026, 5, 5, 12, 0, 0)
        c1 = memory_cards.make_card(
            type="preference", subject="x", content="same content",
            when=when,
        )
        c2 = memory_cards.make_card(
            type="preference", subject="x", content="same content",
            when=when,
        )
        assert c1.id == c2.id  # same content + same day → same id
        p1 = memory_cards.write_card(c1)
        p2 = memory_cards.write_card(c2)
        assert p1 == p2  # same path → second write replaces first

    def test_path_layout(self, isolated_home):
        card = _basic_card()
        path = memory_cards.write_card(card)
        assert path.parent == config.MEMORY_CARDS_DIR
        assert path.name == f"{card.id}.md"

    def test_atomic_write_no_corruption_on_failure(self, isolated_home, monkeypatch):
        # Write a card first.
        card = _basic_card(content="original content")
        memory_cards.write_card(card)
        original_text = memory_cards.card_path(card.id).read_text("utf-8")

        # Now monkeypatch os.replace inside memory_cards to simulate a
        # write failure. (Patching os.replace globally would break tmp
        # cleanup on the same module's failure path.)
        def failing_replace(*a, **kw):
            raise OSError("simulated failure")
        monkeypatch.setattr(memory_cards.os, "replace", failing_replace)

        # Try to write a different card with the SAME id (force overwrite).
        same_id_card = memory_cards.Card(
            id=card.id,
            type="preference",
            subject="coffee",
            content="modified content - should not appear",
            confidence=0.5,
            importance=0.5,
            durability=0.5,
            scope="global",
            created=card.created,
            source=card.source,
        )
        with pytest.raises(OSError):
            memory_cards.write_card(same_id_card)

        # Original file untouched.
        assert memory_cards.card_path(card.id).read_text("utf-8") == original_text
        # No tmp files left dangling (matching `.cards/.<id>.md.*`)
        leftovers = [
            p for p in config.MEMORY_CARDS_DIR.iterdir()
            if p.name.startswith(".")
        ]
        assert leftovers == [], f"leftover tmp files: {leftovers}"

    def test_list_card_paths_empty_when_no_dir(self, isolated_home):
        # cards/ doesn't exist yet
        assert memory_cards.list_card_paths() == []

    def test_list_card_paths_returns_active_cards(self, isolated_home):
        memory_cards.write_card(_basic_card(content="card 1"))
        memory_cards.write_card(_basic_card(content="card 2"))
        paths = memory_cards.list_card_paths()
        assert len(paths) == 2
        assert all(p.suffix == ".md" for p in paths)

    def test_list_card_paths_excludes_superseded_subdir(self, isolated_home):
        c1 = _basic_card(content="will be superseded")
        c2 = _basic_card(content="active card")
        memory_cards.write_card(c1)
        memory_cards.write_card(c2)
        memory_cards.supersede(c1.id)

        paths = memory_cards.list_card_paths()
        assert len(paths) == 1
        assert paths[0].name == f"{c2.id}.md"

    def test_list_card_paths_excludes_dotfiles(self, isolated_home):
        memory_cards.write_card(_basic_card())
        # Manually drop a dotfile (e.g., a leftover tmp from a crashed write)
        config.MEMORY_CARDS_DIR.mkdir(parents=True, exist_ok=True)
        (config.MEMORY_CARDS_DIR / ".tmpfile.md").write_text("junk")
        paths = memory_cards.list_card_paths()
        assert len(paths) == 1

    def test_supersede_moves_card_to_subdir(self, isolated_home):
        c = _basic_card()
        memory_cards.write_card(c)
        original_path = memory_cards.card_path(c.id)
        assert original_path.exists()

        new_path = memory_cards.supersede(c.id)
        assert new_path is not None
        assert not original_path.exists()
        assert new_path.exists()
        assert new_path.parent == config.MEMORY_CARDS_DIR / "_superseded"
        assert new_path.name == original_path.name

    def test_supersede_returns_none_for_missing(self, isolated_home):
        assert memory_cards.supersede("2026-01-01-00000000") is None

    def test_supersede_preserves_content(self, isolated_home):
        c = _basic_card(content="this content matters")
        memory_cards.write_card(c)
        original_text = memory_cards.card_path(c.id).read_text("utf-8")

        new_path = memory_cards.supersede(c.id)
        moved_text = new_path.read_text("utf-8")
        assert moved_text == original_text


# ---------- make_card convenience ----------


class TestMakeCard:
    def test_make_card_derives_id_from_content(self):
        when = _dt.datetime(2026, 5, 5, 10, 0)
        c = memory_cards.make_card(
            type="preference", subject="x", content="hello", when=when,
        )
        assert c.id == memory_cards.card_id("hello", when=when)

    def test_make_card_sets_created_iso_utc(self):
        when = _dt.datetime(2026, 5, 5, 14, 30, 0)
        c = memory_cards.make_card(
            type="preference", subject="x", content="hi", when=when,
        )
        assert c.created == "2026-05-05T14:30:00Z"

    def test_make_card_default_scores_are_neutral(self):
        c = memory_cards.make_card(type="preference", subject="x", content="y")
        assert c.confidence == 0.5
        assert c.importance == 0.5
        assert c.durability == 0.5

    def test_make_card_default_scope_is_global(self):
        # Programmatic-default convenience only. Phase 5 extraction enforces
        # "default = current origin, never auto-promote to global" (see
        # tests/test_memory_privacy.py).
        c = memory_cards.make_card(type="preference", subject="x", content="y")
        assert c.scope == "global"

    def test_make_card_default_source(self):
        c = memory_cards.make_card(type="preference", subject="x", content="y")
        assert c.source.origin_kind == "user_turn"
        assert c.source.turn == 0
        assert c.source.conversation_id == ""

    def test_make_card_with_explicit_source(self):
        src = memory_cards.Source(
            conversation_id="abc", turn=42, gateway="telegram",
            origin_kind="tool_result",
        )
        c = memory_cards.make_card(
            type="preference", subject="x", content="y", source=src,
        )
        assert c.source.origin_kind == "tool_result"
        assert c.source.turn == 42


# ---------- 8 typed categories ----------


class TestTypes:
    def test_eight_types_exact_set(self):
        assert set(memory_cards.TYPES) == {
            "identity", "preference", "goal", "project",
            "habit", "decision", "constraint", "relationship",
        }

    def test_episode_and_reflection_NOT_types(self):
        # Per design tightening: episode + reflection are artifacts, not
        # types. Reflections live in TYPES via origin_kind=consolidation.
        assert "episode" not in memory_cards.TYPES
        assert "reflection" not in memory_cards.TYPES

    @pytest.mark.parametrize("t", list(memory_cards.TYPES))
    def test_each_type_validates(self, t):
        memory_cards.validate(_basic_card(type=t))

    def test_protected_durability_threshold(self):
        # PROTECTED_DURABILITY = 0.7 — cards at/above this are identity-class
        # and never auto-superseded by Phase 5 conflict resolution.
        assert memory_cards.PROTECTED_DURABILITY == 0.7
