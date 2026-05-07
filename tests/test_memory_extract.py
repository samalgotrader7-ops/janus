"""Tests for janus.memory_extract + propose_diff/apply_cards (v1.18.0 Phase 5).

Covers: typed-card parsing, scope upgrade refusal (incl. tool_result
invariant), conflict_resolution semantics, durability protection at
apply time, full propose_diff round-trip with mocked LLM.
"""

from __future__ import annotations
import datetime as _dt
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from janus import (
    config,
    memory,
    memory_cards,
    memory_extract,
    memory_index,
    session_context,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    monkeypatch.setattr(config, "MEMORY_PROTECTED_DURABILITY", 0.7)
    session_context.clear_origin()
    yield home
    session_context.clear_origin()


def _seed_card(content: str, *, type: str = "preference",
               subject: str | None = None, durability: float = 0.5,
               confidence: float = 0.5, scope: str = "global",
               when: _dt.datetime | None = None) -> memory_cards.Card:
    c = memory_cards.make_card(
        type=type,
        subject=subject or content[:20],
        content=content,
        confidence=confidence,
        importance=0.5,
        durability=durability,
        scope=scope,
        when=when,
    )
    memory_cards.write_card(c)
    return c


# ---------- parse_cards: schema validation ----------


class TestParseCards:
    def test_empty_data_returns_empty(self):
        assert memory_extract.parse_cards({}, current_scope="cli") == []
        assert memory_extract.parse_cards(
            {"cards": []}, current_scope="cli",
        ) == []

    def test_basic_valid_card(self):
        data = {"cards": [{
            "type": "preference", "subject": "coffee",
            "content": "black, no sugar",
            "confidence": 0.9, "importance": 0.6, "durability": 0.8,
            "scope": "global",
            "conflict_with": None, "conflict_resolution": "append",
        }]}
        cards = memory_extract.parse_cards(data, current_scope="cli")
        assert len(cards) == 1
        c = cards[0]
        assert c.type == "preference"
        assert c.subject == "coffee"
        assert c.confidence == 0.9
        assert c.scope == "global"

    def test_invalid_type_dropped(self):
        data = {"cards": [{
            "type": "not-a-real-type", "subject": "x", "content": "y",
            "confidence": 0.5, "importance": 0.5, "durability": 0.5,
            "scope": "cli",
        }]}
        assert memory_extract.parse_cards(data, current_scope="cli") == []

    def test_missing_subject_dropped(self):
        data = {"cards": [{
            "type": "preference", "subject": "", "content": "y",
            "confidence": 0.5, "importance": 0.5, "durability": 0.5,
            "scope": "cli",
        }]}
        assert memory_extract.parse_cards(data, current_scope="cli") == []

    def test_missing_content_dropped(self):
        data = {"cards": [{
            "type": "preference", "subject": "x", "content": "",
            "confidence": 0.5, "importance": 0.5, "durability": 0.5,
            "scope": "cli",
        }]}
        assert memory_extract.parse_cards(data, current_scope="cli") == []

    def test_score_clamped_to_01_range(self):
        data = {"cards": [{
            "type": "preference", "subject": "x", "content": "y",
            "confidence": 1.5,    # over → clamp to 1.0
            "importance": -0.3,    # under → clamp to 0.0
            "durability": 0.5,
            "scope": "cli",
        }]}
        cards = memory_extract.parse_cards(data, current_scope="cli")
        assert len(cards) == 1
        assert cards[0].confidence == 1.0
        assert cards[0].importance == 0.0

    def test_invalid_score_type_dropped(self):
        data = {"cards": [{
            "type": "preference", "subject": "x", "content": "y",
            "confidence": "not_a_number",
            "importance": 0.5, "durability": 0.5,
            "scope": "cli",
        }]}
        assert memory_extract.parse_cards(data, current_scope="cli") == []

    def test_default_scope_when_missing(self, monkeypatch):
        """When the model omits scope: in multi-user mode (single-user
        OFF) it defaults to current_scope; in single-user mode (default
        ON since v1.25.2) it defaults to global for user_turn cards.
        This test pins the multi-user behavior; v1.25.2's
        test_single_user_mode.py pins the single-user behavior."""
        monkeypatch.setattr(config, "MEMORY_SINGLE_USER", False)
        data = {"cards": [{
            "type": "preference", "subject": "x", "content": "y",
            "confidence": 0.5, "importance": 0.5, "durability": 0.5,
            # no scope field — should default to current_scope
        }]}
        cards = memory_extract.parse_cards(data, current_scope="telegram:42")
        assert cards[0].scope == "telegram:42"

    def test_invalid_scope_dropped(self):
        data = {"cards": [{
            "type": "preference", "subject": "x", "content": "y",
            "confidence": 0.5, "importance": 0.5, "durability": 0.5,
            "scope": "weird:::scope",
        }]}
        assert memory_extract.parse_cards(data, current_scope="cli") == []

    def test_max_5_cards_per_call(self):
        data = {"cards": [
            {
                "type": "preference", "subject": f"sub_{i}",
                "content": f"content_{i}",
                "confidence": 0.5, "importance": 0.5, "durability": 0.5,
                "scope": "cli",
            }
            for i in range(20)
        ]}
        cards = memory_extract.parse_cards(data, current_scope="cli")
        assert len(cards) == 5

    def test_unknown_resolution_defaults_to_append(self):
        data = {"cards": [{
            "type": "preference", "subject": "x", "content": "y",
            "confidence": 0.5, "importance": 0.5, "durability": 0.5,
            "scope": "cli", "conflict_resolution": "wat",
        }]}
        cards = memory_extract.parse_cards(data, current_scope="cli")
        assert cards[0].conflict_resolution == "append"


# ---------- Scope-upgrade refusal (privacy invariant) ----------


class TestScopeUpgradeRefusal:
    def test_tool_result_origin_cannot_promote_to_global(self):
        data = {"cards": [{
            "type": "preference", "subject": "x", "content": "y",
            "confidence": 0.5, "importance": 0.5, "durability": 0.5,
            "scope": "global",  # extractor tries to write global
        }]}
        cards = memory_extract.parse_cards(
            data,
            current_scope="telegram:42",
            origin_kind="tool_result",
        )
        # Scope downgraded to current_scope
        assert cards[0].scope == "telegram:42"

    def test_user_turn_origin_can_set_global(self):
        # When the user explicitly says "remember globally", the user_turn
        # origin allows the model to set scope=global.
        data = {"cards": [{
            "type": "preference", "subject": "x", "content": "y",
            "confidence": 0.5, "importance": 0.5, "durability": 0.5,
            "scope": "global",
        }]}
        cards = memory_extract.parse_cards(
            data,
            current_scope="telegram:42",
            origin_kind="user_turn",
        )
        assert cards[0].scope == "global"


# ---------- render_existing_cards_block ----------


class TestExistingCardsBlock:
    def test_empty_when_no_cards(self, isolated_home):
        block = memory_extract.render_existing_cards_block()
        assert "no cards yet" in block.lower() or block == "(no cards yet)"

    def test_includes_recent_cards(self, isolated_home):
        _seed_card("first", subject="alpha", type="preference",
                   when=_dt.datetime(2026, 5, 1))
        _seed_card("second", subject="beta", type="habit",
                   when=_dt.datetime(2026, 5, 5))
        memory_index.reconcile()

        block = memory_extract.render_existing_cards_block()
        assert "alpha" in block
        assert "beta" in block
        assert "preference" in block
        assert "habit" in block

    def test_limit_caps_output(self, isolated_home):
        for i in range(20):
            _seed_card(f"content {i}", subject=f"sub_{i}")
        memory_index.reconcile()
        block = memory_extract.render_existing_cards_block(limit=5)
        # 5 cards listed
        assert block.count("\n- id=") == 5 or block.count("- id=") == 5


# ---------- apply_cards: conflict resolution ----------


class TestApplyCards:
    def test_basic_apply_writes_card(self, isolated_home):
        proposals = [memory_extract.CardProposal(
            type="preference", subject="coffee", content="black",
            confidence=0.9, importance=0.5, durability=0.5,
            scope="cli",
        )]
        written = memory.apply_cards(proposals, gateway="cli")
        assert len(written) == 1
        # Card on disk
        path = memory_cards.card_path(written[0])
        assert path.exists()

    def test_apply_ignore_drops_card(self, isolated_home):
        proposals = [memory_extract.CardProposal(
            type="preference", subject="x", content="y",
            confidence=0.9, importance=0.5, durability=0.5,
            scope="cli", conflict_resolution="ignore",
        )]
        written = memory.apply_cards(proposals, gateway="cli")
        assert written == []

    def test_apply_replace_supersedes_existing(self, isolated_home):
        # Pre-existing card with low durability (NOT protected)
        old = _seed_card("old fact", subject="coffee", durability=0.4)
        memory_index.reconcile()

        proposals = [memory_extract.CardProposal(
            type="preference", subject="coffee", content="new fact",
            confidence=0.9, importance=0.5, durability=0.5,
            scope="global", conflict_resolution="replace",
            conflict_with=old.id,
        )]
        written = memory.apply_cards(proposals, gateway="cli")
        assert len(written) == 1
        # Old card moved to _superseded
        assert not memory_cards.card_path(old.id).exists()
        sup = config.MEMORY_CARDS_DIR / "_superseded" / f"{old.id}.md"
        assert sup.exists()

    def test_apply_replace_blocked_for_protected_durability(self, isolated_home):
        # Pre-existing card with HIGH durability (>= 0.7 — protected)
        old = _seed_card(
            "identity-class fact", subject="coffee",
            durability=0.9,
        )
        memory_index.reconcile()

        proposals = [memory_extract.CardProposal(
            type="preference", subject="coffee",
            content="trying to override identity",
            confidence=0.9, importance=0.5, durability=0.5,
            scope="global", conflict_resolution="replace",
            conflict_with=old.id,
        )]
        written = memory.apply_cards(proposals, gateway="cli")
        # The new card IS written (replace falls through to append-style)
        assert len(written) == 1
        # The OLD card stays put — protected
        assert memory_cards.card_path(old.id).exists()
        # Both exist in DB
        memory_index.reconcile()
        rows = memory_index.lookup_by_subject("preference", "coffee")
        assert len(rows) == 2

    def test_apply_mark_uncertain_clamps_confidence(self, isolated_home):
        proposals = [memory_extract.CardProposal(
            type="preference", subject="x", content="y",
            confidence=0.95, importance=0.5, durability=0.5,
            scope="cli", conflict_resolution="mark_uncertain",
        )]
        written = memory.apply_cards(proposals, gateway="cli")
        assert len(written) == 1
        # Read back and confirm confidence was clamped to ≤ 0.5
        card = memory_cards.read_card(memory_cards.card_path(written[0]))
        assert card.confidence <= 0.5

    def test_apply_attaches_provenance(self, isolated_home):
        proposals = [memory_extract.CardProposal(
            type="preference", subject="x", content="y",
            confidence=0.5, importance=0.5, durability=0.5,
            scope="cli",
        )]
        written = memory.apply_cards(
            proposals,
            conversation_id="conv_abc",
            turn=42,
            gateway="cli",
        )
        card = memory_cards.read_card(memory_cards.card_path(written[0]))
        assert card.source.conversation_id == "conv_abc"
        assert card.source.turn == 42
        assert card.source.gateway == "cli"
        assert card.source.origin_kind == "user_turn"

    def test_apply_invalid_card_skipped(self, isolated_home):
        # A card with an invalid scope should be skipped at apply time
        # (defense-in-depth — parse_cards already filters, but apply is
        # the last line).
        proposals = [
            memory_extract.CardProposal(
                type="preference", subject="x", content="y",
                confidence=0.5, importance=0.5, durability=0.5,
                scope="impossible:scope:format",
            ),
            memory_extract.CardProposal(
                type="preference", subject="z", content="valid",
                confidence=0.5, importance=0.5, durability=0.5,
                scope="cli",
            ),
        ]
        written = memory.apply_cards(proposals, gateway="cli")
        assert len(written) == 1  # second one only

    def test_apply_inline_indexes_new_cards(self, isolated_home):
        proposals = [memory_extract.CardProposal(
            type="preference", subject="findme", content="searchable text",
            confidence=0.5, importance=0.5, durability=0.5,
            scope="global",
        )]
        memory.apply_cards(proposals, gateway="cli")
        # Index should be populated immediately — recall finds it without
        # waiting for next reconcile.
        rows = memory_index.lookup_by_subject("preference", "findme")
        assert len(rows) == 1


# ---------- propose_diff full round-trip with mocked LLM ----------


class TestProposeDiffEnd2End:
    def test_returns_dict_with_ops_and_cards(self, isolated_home, monkeypatch):
        fake_response = {
            "ops": [],
            "cards": [{
                "type": "preference", "subject": "coffee",
                "content": "black, no sugar",
                "confidence": 0.9, "importance": 0.6, "durability": 0.8,
                "scope": "global",
                "conflict_with": None, "conflict_resolution": "append",
            }],
        }

        def fake_chat(**kwargs):
            return {"role": "assistant",
                    "content": json.dumps(fake_response)}

        monkeypatch.setattr(memory, "_chat_with_model", fake_chat)
        result = memory.propose_diff(
            "I prefer coffee black",
            "Got it.",
        )
        assert isinstance(result, dict)
        assert "ops" in result
        assert "cards" in result
        assert len(result["cards"]) == 1
        assert result["cards"][0].subject == "coffee"

    def test_returns_empty_when_disabled(self, isolated_home, monkeypatch):
        monkeypatch.setattr(config, "MEMORY_PROPOSE_ENABLED", False)
        result = memory.propose_diff("anything", "anything")
        assert result == {"ops": [], "cards": []}

    def test_malformed_response_falls_back_safely(
        self, isolated_home, monkeypatch
    ):
        def bad_chat(**kwargs):
            return {"role": "assistant", "content": "not json at all"}

        monkeypatch.setattr(memory, "_chat_with_model", bad_chat)
        result = memory.propose_diff("x", "y")
        assert result == {"ops": [], "cards": []}

    def test_full_pipeline_extract_apply_recall(
        self, isolated_home, monkeypatch
    ):
        # End-to-end: extract → apply → next turn's recall finds the card.
        from janus import memory_recall as _mr
        _mr.reset_reconcile_flag()

        fake_response = {
            "ops": [],
            "cards": [{
                "type": "preference", "subject": "tabs_vs_spaces",
                "content": "Sam uses tabs for indent",
                "confidence": 0.95, "importance": 0.7, "durability": 0.8,
                "scope": "global",
                "conflict_with": None, "conflict_resolution": "append",
            }],
        }

        def fake_chat(**kwargs):
            return {"role": "assistant",
                    "content": json.dumps(fake_response)}

        monkeypatch.setattr(memory, "_chat_with_model", fake_chat)
        result = memory.propose_diff(
            "I always use tabs",
            "Noted.",
        )
        memory.apply_cards(result["cards"], gateway="cli")

        # Next "turn": recall against a related query
        cards = _mr.top_k("indentation tabs", current_scope="global")
        assert len(cards) >= 1
        assert any(c["subject"] == "tabs_vs_spaces" for c in cards)
