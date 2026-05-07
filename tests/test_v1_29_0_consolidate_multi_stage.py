"""Tests for v1.29.0 — memory consolidation via swarm (Phase 4).

Multi-stage variant of memory_consolidate.run_once:
  Stage 1 (parallel): per-type pattern extraction
  Stage 2 (single):   cross-type synthesis

Tested with mocked LLM so the suite stays fast and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from janus import (
    config, memory, memory_cards, memory_consolidate, memory_index,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Same fixture pattern as test_memory_consolidate.py — patch all
    four memory-related config paths so apply_cards / read_card /
    list_all all point at the temp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "MEMORY_CARDS_DIR", home / "memory" / "cards")
    monkeypatch.setattr(config, "MEMORY_INDEX_DB", home / "memory" / "index.db")
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    yield home


def _seed_card(*, type_: str, subject: str, content: str) -> None:
    """Drop a card into the cards dir + reconcile so list_all sees it.

    Uses memory_cards.write_card directly (mirrors the pattern in
    test_memory_consolidate.py) — apply_cards routes through the
    typed-card extractor and would re-validate, which we don't need
    for fixture seeding.
    """
    c = memory_cards.make_card(
        type=type_, subject=subject, content=content,
        confidence=0.8, importance=0.6, durability=0.5,
        scope="global",
    )
    memory_cards.write_card(c)


# ============================================================
# Stage 1 unit
# ============================================================


def test_stage1_returns_empty_for_empty_cards(isolated_home):
    out = memory_consolidate._extract_patterns_for_type("habit", [])
    assert out == []


def test_stage1_calls_llm_with_type_specific_prompt(isolated_home, monkeypatch):
    captured = {}

    def fake_chat(**kw):
        captured["messages"] = kw["messages"]
        return {
            "role": "assistant",
            "content": json.dumps({"patterns": ["pattern A", "pattern B"]}),
        }

    monkeypatch.setattr(memory, "_chat_with_model", fake_chat)
    out = memory_consolidate._extract_patterns_for_type(
        "habit", [{"id": "1", "subject": "x", "content": "y"}],
    )
    assert out == ["pattern A", "pattern B"]
    # The system prompt mentions the within-type focus
    sys_msg = captured["messages"][0]["content"]
    assert "within-type" in sys_msg.lower() or "ONE type" in sys_msg
    # The user message includes the type name
    user_msg = captured["messages"][1]["content"]
    assert "habit" in user_msg


def test_stage1_caps_at_5_patterns(isolated_home, monkeypatch):
    monkeypatch.setattr(
        memory, "_chat_with_model",
        lambda **kw: {
            "role": "assistant",
            "content": json.dumps({
                "patterns": [f"p{i}" for i in range(20)],
            }),
        },
    )
    out = memory_consolidate._extract_patterns_for_type(
        "habit", [{"id": "1"}],
    )
    assert len(out) == 5


def test_stage1_truncates_long_pattern_strings(isolated_home, monkeypatch):
    monkeypatch.setattr(
        memory, "_chat_with_model",
        lambda **kw: {
            "role": "assistant",
            "content": json.dumps({"patterns": ["x" * 1000]}),
        },
    )
    out = memory_consolidate._extract_patterns_for_type(
        "habit", [{"id": "1"}],
    )
    assert len(out[0]) <= 400


def test_stage1_handles_invalid_json(isolated_home, monkeypatch):
    monkeypatch.setattr(
        memory, "_chat_with_model",
        lambda **kw: {"role": "assistant", "content": "not json"},
    )
    out = memory_consolidate._extract_patterns_for_type(
        "habit", [{"id": "1"}],
    )
    assert out == []


def test_stage1_handles_missing_patterns_key(isolated_home, monkeypatch):
    monkeypatch.setattr(
        memory, "_chat_with_model",
        lambda **kw: {"role": "assistant", "content": json.dumps({})},
    )
    out = memory_consolidate._extract_patterns_for_type(
        "habit", [{"id": "1"}],
    )
    assert out == []


def test_stage1_handles_llm_exception(isolated_home, monkeypatch):
    def boom(**kw):
        raise RuntimeError("network")
    monkeypatch.setattr(memory, "_chat_with_model", boom)
    out = memory_consolidate._extract_patterns_for_type(
        "habit", [{"id": "1"}],
    )
    assert out == []


def test_stage1_skips_non_string_patterns(isolated_home, monkeypatch):
    monkeypatch.setattr(
        memory, "_chat_with_model",
        lambda **kw: {
            "role": "assistant",
            "content": json.dumps({
                "patterns": ["good", 42, None, "", "  ", "also good"],
            }),
        },
    )
    out = memory_consolidate._extract_patterns_for_type(
        "habit", [{"id": "1"}],
    )
    assert out == ["good", "also good"]


# ============================================================
# run_multi_stage end-to-end
# ============================================================


def test_run_multi_stage_returns_zero_for_few_cards(isolated_home):
    out = memory_consolidate.run_multi_stage()
    assert out["examined"] < 3
    assert out["written"] == 0
    assert out["stages"] == 0


def test_run_multi_stage_skips_stage2_when_no_patterns(isolated_home, monkeypatch):
    """Stage 1 returns no patterns anywhere → don't waste a stage-2 call."""
    for i in range(5):
        _seed_card(type_="habit", subject=f"h{i}", content=f"content {i}")
    memory_index.reconcile()

    monkeypatch.setattr(
        memory, "_chat_with_model",
        lambda **kw: {
            "role": "assistant",
            "content": json.dumps({"patterns": []}),
        },
    )
    out = memory_consolidate.run_multi_stage()
    assert out["written"] == 0
    assert out["stages"] == 1  # stage 1 only


def test_run_multi_stage_full_path(isolated_home, monkeypatch):
    """3+ cards across 2 types → stage 1 finds patterns → stage 2
    synthesizes → cards written."""
    # Seed 4 habit cards + 3 project cards
    for i in range(4):
        _seed_card(
            type_="habit", subject=f"h{i}",
            content=f"morning routine: {i}",
        )
    for i in range(3):
        _seed_card(
            type_="project", subject=f"p{i}",
            content=f"working on x{i}",
        )
    memory_index.reconcile()

    call_count = {"n": 0}

    def fake_chat(**kw):
        call_count["n"] += 1
        sys_msg = kw["messages"][0]["content"]
        # Stage 1 system prompt analyzes ONE type; stage 2 synthesizes
        # cross-type. Use the cross-type phrase (stage-2-only) to
        # discriminate — "within-type" appears in both prompts.
        if "synthesize cross-type" not in sys_msg.lower():
            # Stage 1
            return {
                "role": "assistant",
                "content": json.dumps({
                    "patterns": ["routine pattern detected"],
                }),
            }
        # Stage 2
        return {
            "role": "assistant",
            "content": json.dumps({
                "cards": [{
                    "type": "habit", "subject": "morning_blocks",
                    "content": "synthesis of routine + project",
                    "confidence": 0.9, "importance": 0.7, "durability": 0.8,
                    "scope": "global",
                    "conflict_with": None,
                    "conflict_resolution": "append",
                }],
            }),
        }

    monkeypatch.setattr(memory, "_chat_with_model", fake_chat)
    out = memory_consolidate.run_multi_stage()
    # 2 types × 1 stage-1 call + 1 stage-2 call = 3 LLM calls total
    assert call_count["n"] == 3
    assert out["stages"] == 2
    assert out["written"] == 1
    # patterns_per_type captured BOTH types
    assert "habit" in out["patterns_per_type"]
    assert "project" in out["patterns_per_type"]


def test_run_multi_stage_stage2_garbage_returns_zero(isolated_home, monkeypatch):
    """Stage 1 finds patterns; stage 2 returns invalid JSON →
    written=0 but stages=2 (we did try)."""
    # Unique content per card so write_card's content-hash id doesn't
    # collapse them into one (make_card derives id from content+now).
    for i in range(5):
        _seed_card(type_="habit", subject=f"h{i}", content=f"unique-{i}")
    memory_index.reconcile()

    def fake_chat(**kw):
        sys_msg = kw["messages"][0]["content"]
        # Stage 1 system prompt analyzes ONE type; stage 2 synthesizes
        # cross-type. Use the cross-type phrase (stage-2-only) to
        # discriminate — "within-type" appears in both prompts.
        if "synthesize cross-type" not in sys_msg.lower():
            return {
                "role": "assistant",
                "content": json.dumps({"patterns": ["p"]}),
            }
        return {"role": "assistant", "content": "not json"}

    monkeypatch.setattr(memory, "_chat_with_model", fake_chat)
    out = memory_consolidate.run_multi_stage()
    assert out["written"] == 0
    assert out["stages"] == 2


def test_run_multi_stage_runs_stage1_in_parallel(isolated_home, monkeypatch):
    """Sentinel: stage 1 calls happen on different threads (proves
    ThreadPoolExecutor usage). Each call records its thread id."""
    import threading
    # Unique content per card so the content-hash id doesn't dedupe.
    for i in range(4):
        _seed_card(type_="habit", subject=f"h{i}", content=f"hu{i}")
    for i in range(3):
        _seed_card(type_="project", subject=f"p{i}", content=f"pu{i}")
    for i in range(3):
        _seed_card(type_="goal", subject=f"g{i}", content=f"gu{i}")
    memory_index.reconcile()

    seen_threads: set[int] = set()
    barrier = threading.Barrier(3, timeout=5)  # 3 types

    def fake_chat(**kw):
        sys_msg = kw["messages"][0]["content"]
        # Stage 1 system prompt analyzes ONE type; stage 2 synthesizes
        # cross-type. Use the cross-type phrase (stage-2-only) to
        # discriminate — "within-type" appears in both prompts.
        if "synthesize cross-type" not in sys_msg.lower():
            try:
                barrier.wait()  # all stage-1 threads must reach this
            except threading.BrokenBarrierError:
                pass
            seen_threads.add(threading.get_ident())
            return {
                "role": "assistant",
                "content": json.dumps({"patterns": []}),
            }
        return {"role": "assistant", "content": json.dumps({"cards": []})}

    monkeypatch.setattr(memory, "_chat_with_model", fake_chat)
    memory_consolidate.run_multi_stage()
    # Multiple threads were used — barrier wouldn't unblock with 1.
    assert len(seen_threads) >= 2


# ============================================================
# Source-pin: cli_rich --multi-stage
# ============================================================


def test_cli_rich_consolidate_supports_multi_stage_flag():
    """Source-pin: /memory consolidate --multi-stage routes to
    run_multi_stage."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "--multi-stage" in src
    assert "run_multi_stage" in src


def test_cli_rich_consolidate_respects_env_strategy():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "JANUS_CONSOLIDATE_STRATEGY" in src


def test_cli_rich_consolidate_renders_stages_summary():
    """Source-pin: when stages > 0 we render a one-line breakdown
    showing patterns-per-type counts."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "patterns_per_type" in src
    assert "stages" in src.lower()


# ============================================================
# Existing run_once stays unchanged
# ============================================================


def test_run_once_still_works(isolated_home, monkeypatch):
    """Don't regress the v1.18 path. Single-call consolidation
    still functions."""
    for i in range(5):
        _seed_card(type_="habit", subject=f"h{i}", content="x")
    memory_index.reconcile()

    monkeypatch.setattr(
        memory, "_chat_with_model",
        lambda **kw: {
            "role": "assistant",
            "content": json.dumps({"cards": []}),
        },
    )
    out = memory_consolidate.run_once()
    assert "examined" in out
    assert "written" in out
    # No "stages" key on the v1.18 path — the new key is multi-stage only
    assert "stages" not in out
