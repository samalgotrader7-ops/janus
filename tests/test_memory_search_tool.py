"""Tests for the memory_search model-callable tool (v1.18.0 Phase 6)."""

from __future__ import annotations
import datetime as _dt

import pytest

from janus import config, memory_cards, memory_index, memory_recall, session_context
from janus.tools.memory_search import MemorySearch


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    memory_recall.reset_reconcile_flag()
    session_context.clear_origin()
    yield home
    session_context.clear_origin()


def _approve(*args, **kwargs):
    return True


def _seed(content: str, *, type: str = "preference",
          subject: str | None = None, scope: str = "global") -> memory_cards.Card:
    c = memory_cards.make_card(
        type=type, subject=subject or content[:20],
        content=content,
        confidence=0.7, importance=0.5, durability=0.5,
        scope=scope,
    )
    memory_cards.write_card(c)
    return c


# ---------- Tool surface ----------


class TestSurface:
    def test_metadata(self):
        t = MemorySearch()
        assert t.name == "memory_search"
        assert t.risk == "read"
        assert t.dangerous is False
        # Required: query
        assert "query" in t.parameters["required"]
        # Optional: types, scope, top_k
        props = t.parameters["properties"]
        assert "types" in props
        assert "scope" in props
        assert "top_k" in props


# ---------- Behavior ----------


class TestBehavior:
    def test_empty_query_returns_error(self, isolated_home):
        out = MemorySearch().run({"query": ""}, _approve)
        assert "error" in out.lower()

    def test_no_results_returns_friendly_message(self, isolated_home):
        out = MemorySearch().run({"query": "zebra"}, _approve)
        assert "no matching" in out.lower()

    def test_basic_search_returns_card(self, isolated_home):
        _seed("I prefer my coffee black no sugar", subject="coffee_pref")
        out = MemorySearch().run(
            {"query": "coffee", "scope": "global"}, _approve,
        )
        assert "coffee_pref" in out
        assert "[preference:coffee_pref]" in out
        assert "id=" in out  # metadata included

    def test_top_k_caps(self, isolated_home):
        for i in range(10):
            _seed(f"keyword content {i}", subject=f"sub_{i}")
        out = MemorySearch().run(
            {"query": "keyword", "scope": "global", "top_k": 3},
            _approve,
        )
        # Only 3 entries should appear
        assert out.count("[preference:") == 3

    def test_top_k_clamped_to_max(self, isolated_home):
        for i in range(30):
            _seed(f"keyword content {i}", subject=f"sub_{i}")
        out = MemorySearch().run(
            {"query": "keyword", "scope": "global", "top_k": 100},
            _approve,
        )
        # Capped at 20
        assert out.count("[preference:") <= 20

    def test_top_k_minimum_one(self, isolated_home):
        _seed("hello", subject="x")
        out = MemorySearch().run(
            {"query": "hello", "scope": "global", "top_k": 0},
            _approve,
        )
        # Even top_k=0 gets clamped to 1
        assert "[preference:x]" in out

    def test_types_filter(self, isolated_home):
        _seed("pref content here", subject="x", type="preference")
        _seed("habit content here", subject="y", type="habit")
        out = MemorySearch().run(
            {"query": "content", "scope": "global", "types": ["habit"]},
            _approve,
        )
        # Only habit shows
        assert "[habit:" in out
        assert "[preference:" not in out

    def test_invalid_type_rejected(self, isolated_home):
        out = MemorySearch().run(
            {"query": "x", "types": ["not_a_type"]},
            _approve,
        )
        assert "error" in out.lower()
        assert "invalid" in out.lower()

    def test_types_must_be_list(self, isolated_home):
        out = MemorySearch().run(
            {"query": "x", "types": "preference"},  # string, not list
            _approve,
        )
        assert "error" in out.lower()

    def test_scope_filter_isolates_telegram_chat(self, isolated_home):
        _seed("global fact here", subject="x", scope="global")
        _seed("private to chat 42", subject="y", scope="telegram:42")
        # Search from chat 99 — telegram:42 card invisible
        out = MemorySearch().run(
            {"query": "private", "scope": "telegram:99"},
            _approve,
        )
        assert "no matching" in out.lower()
        # Search from chat 42 — visible
        out = MemorySearch().run(
            {"query": "private", "scope": "telegram:42"},
            _approve,
        )
        assert "[preference:y]" in out

    def test_default_scope_uses_current_origin(self, isolated_home, monkeypatch):
        # When scope kwarg omitted, defaults to session_context.current_scope()
        monkeypatch.setattr(config, "WORKSPACE", isolated_home)
        session_context.set_origin(platform="telegram", chat_id="42")
        _seed("private to 42", subject="x", scope="telegram:42")
        out = MemorySearch().run({"query": "private"}, _approve)
        assert "[preference:x]" in out

    def test_bumps_recall_count(self, isolated_home):
        c = _seed("bump-me content", subject="x")
        MemorySearch().run(
            {"query": "bump-me", "scope": "global"}, _approve,
        )
        # The pre-existing top_k_block path would also bump. Either way, the
        # tool's call should have caused at least one bump.
        stats = memory_index.get_stats(c.id)
        assert stats is not None
        assert stats["recall_count"] >= 1

    def test_output_capped_at_max_bytes(self, isolated_home):
        # Pile in many large-content cards
        for i in range(20):
            _seed("anchor " + ("x" * 1000), subject=f"sub_{i}")
        out = MemorySearch().run(
            {"query": "anchor", "scope": "global", "top_k": 20},
            _approve,
        )
        # Output capped (even with 20 huge cards)
        assert len(out) <= 2000


# ---------- Registration ----------


class TestRegistration:
    def test_in_default_registry(self):
        from janus.tools import default_registry
        reg = default_registry()
        # Tool registered by name
        names = [s["function"]["name"] for s in reg.schemas()]
        assert "memory_search" in names

    def test_filter_via_tool_names(self):
        from janus.tools import default_registry
        reg = default_registry(tool_names=["memory_search"])
        names = [s["function"]["name"] for s in reg.schemas()]
        assert "memory_search" in names
        # Other tools filtered out
        assert "fs_write" not in names
