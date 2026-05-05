"""Tests for janus.memory_consolidate (v1.18.0 Phase 8 manual reflection)."""

from __future__ import annotations
import json

import pytest

from janus import config, memory, memory_cards, memory_consolidate, memory_index


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    yield home


def _seed(content: str, *, type: str = "preference",
          subject: str | None = None) -> memory_cards.Card:
    c = memory_cards.make_card(
        type=type, subject=subject or content[:20],
        content=content, confidence=0.7, importance=0.5, durability=0.5,
        scope="global",
    )
    memory_cards.write_card(c)
    return c


# ---------- run_once basic flow ----------


class TestConsolidateRunOnce:
    def test_skip_when_too_few_cards(self, isolated_home):
        # < 3 cards → no LLM call, return immediately.
        _seed("only one card", subject="x")
        result = memory_consolidate.run_once()
        assert result["written"] == 0
        assert result["examined"] <= 2

    def test_calls_llm_with_card_summaries(self, isolated_home, monkeypatch):
        for i in range(5):
            _seed(f"content-{i} about coffee preferences", subject=f"sub_{i}")
        memory_index.reconcile()

        captured = {}
        def fake_chat(**kwargs):
            captured["messages"] = kwargs["messages"]
            return {
                "role": "assistant",
                "content": json.dumps({"cards": []}),
            }
        monkeypatch.setattr(memory, "_chat_with_model", fake_chat)

        memory_consolidate.run_once()
        # The LLM saw ALL 5 card summaries.
        user_msg = captured["messages"][1]["content"]
        for i in range(5):
            assert f"sub_{i}" in user_msg

    def test_writes_reflection_cards_with_consolidation_origin(
        self, isolated_home, monkeypatch
    ):
        for i in range(5):
            _seed(f"content-{i}", subject=f"sub_{i}")
        memory_index.reconcile()

        fake_response = {"cards": [{
            "type": "preference",
            "subject": "synthesis_pattern",
            "content": "Consolidated pattern across 5 cards",
            "confidence": 0.9,
            "importance": 0.8,
            "durability": 0.8,
            "scope": "global",
            "conflict_with": None,
            "conflict_resolution": "append",
        }]}
        monkeypatch.setattr(
            memory, "_chat_with_model",
            lambda **kw: {
                "role": "assistant",
                "content": json.dumps(fake_response),
            },
        )

        result = memory_consolidate.run_once()
        assert result["written"] == 1
        # Find the new card and verify origin_kind
        rows = memory_index.list_all()
        synthesis = next(r for r in rows if r["subject"] == "synthesis_pattern")
        from pathlib import Path
        card = memory_cards.read_card(Path(synthesis["path"]))
        assert card.source.origin_kind == "consolidation"

    def test_handles_malformed_llm_response(self, isolated_home, monkeypatch):
        for i in range(5):
            _seed(f"content-{i}", subject=f"sub_{i}")
        memory_index.reconcile()
        monkeypatch.setattr(
            memory, "_chat_with_model",
            lambda **kw: {"role": "assistant", "content": "not json"},
        )
        result = memory_consolidate.run_once()
        assert result["written"] == 0
        # Doesn't crash

    def test_caps_input_cards(self, isolated_home, monkeypatch):
        for i in range(20):
            _seed(f"content-{i}", subject=f"sub_{i}")
        memory_index.reconcile()

        captured_user_msg = {}
        def fake_chat(**kwargs):
            captured_user_msg["msg"] = kwargs["messages"][1]["content"]
            return {"role": "assistant", "content": json.dumps({"cards": []})}
        monkeypatch.setattr(memory, "_chat_with_model", fake_chat)

        # Run with cap of 5
        memory_consolidate.run_once(max_input_cards=5)
        msg = captured_user_msg["msg"]
        # We see "(N of 20 total)" → confirm cap applied
        assert "of 20 total" in msg
        # And only 5 sub_* substrings of the limited set should appear:
        # the rest of the 20 are NOT in the message
        sub_count = sum(1 for i in range(20) if f"sub_{i}" in msg)
        assert sub_count == 5


# ---------- Origin enforcement ----------


class TestConsolidationOrigin:
    def test_origin_kind_consolidation_passed_through(self, isolated_home, monkeypatch):
        for i in range(5):
            _seed(f"content-{i}", subject=f"sub_{i}")
        memory_index.reconcile()

        # Try to inject a "global" scope from a clearly synthesizing model.
        # origin_kind=consolidation: scope=global IS allowed (not tool_result).
        fake_response = {"cards": [{
            "type": "habit", "subject": "early_riser",
            "content": "synthesis",
            "confidence": 0.8, "importance": 0.7, "durability": 0.8,
            "scope": "global",
            "conflict_with": None, "conflict_resolution": "append",
        }]}
        monkeypatch.setattr(
            memory, "_chat_with_model",
            lambda **kw: {"role": "assistant", "content": json.dumps(fake_response)},
        )
        result = memory_consolidate.run_once()
        assert result["written"] == 1
        rows = memory_index.list_all()
        synth = next(r for r in rows if r["subject"] == "early_riser")
        assert synth["scope"] == "global"
