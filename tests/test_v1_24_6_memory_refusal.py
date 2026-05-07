"""Tests for v1.24.6 #4 — capture user-refusal events as constraint
memory cards.

Sam's 2026-05-07 7:26 AM session: he refused
``fs_write docs/SWARM_EXPLAINER.md``. Pre-1.24.6, that veto was lost
— next session the model could try again. v1.24.6 scans the trace
post-turn for "refused by user:" tool results and writes a
constraint card automatically (no LLM call, no approval prompt —
the user's click WAS the consent gesture).
"""
from __future__ import annotations


# ---------- extract_refusals ----------


def test_extract_picks_up_fs_write_refusal():
    from janus import memory_refusal
    trace = [
        {
            "step": 1, "type": "tool_call", "name": "fs_write",
            "args": {"path": "docs/SWARM_EXPLAINER.md", "content": "..."},
            "result_preview": "refused by user: write to docs/SWARM_EXPLAINER.md",
        }
    ]
    refs = memory_refusal.extract_refusals(trace)
    assert len(refs) == 1
    assert refs[0].tool == "fs_write"
    assert refs[0].target == "docs/SWARM_EXPLAINER.md"


def test_extract_picks_up_fs_edit_refusal():
    from janus import memory_refusal
    trace = [
        {
            "name": "fs_edit",
            "args": {"path": "docs/ARCHITECTURE.md",
                     "old_string": "x", "new_string": "y"},
            "result_preview": "refused by user: edit docs/ARCHITECTURE.md",
        }
    ]
    refs = memory_refusal.extract_refusals(trace)
    assert len(refs) == 1
    assert refs[0].tool == "fs_edit"


def test_extract_picks_up_shell_refusal():
    from janus import memory_refusal
    trace = [
        {
            "name": "shell",
            "args": {"command": "rm -rf /tmp/important"},
            "result_preview": "refused by user: shell command",
        }
    ]
    refs = memory_refusal.extract_refusals(trace)
    assert len(refs) == 1
    assert refs[0].tool == "shell"
    assert refs[0].target == "rm -rf /tmp/important"


def test_extract_ignores_successful_calls():
    from janus import memory_refusal
    trace = [
        {"name": "fs_read", "args": {"path": "x.py"},
         "result_preview": "file contents..."},
        {"name": "fs_write", "args": {"path": "y.md"},
         "result_preview": "wrote 100 bytes to y.md"},
    ]
    assert memory_refusal.extract_refusals(trace) == []


def test_extract_ignores_errors_that_arent_refusals():
    from janus import memory_refusal
    trace = [
        {"name": "fs_edit", "args": {"path": "x.py"},
         "result_preview": "error: old_string not found in x.py"},
        {"name": "shell", "args": {"command": "ls"},
         "result_preview": "exit=1: ls: cannot access"},
    ]
    assert memory_refusal.extract_refusals(trace) == []


def test_extract_handles_empty_trace():
    from janus import memory_refusal
    assert memory_refusal.extract_refusals([]) == []
    assert memory_refusal.extract_refusals(None) == []  # type: ignore[arg-type]


def test_extract_picks_up_multiple_refusals_in_order():
    from janus import memory_refusal
    trace = [
        {"name": "fs_write", "args": {"path": "a.md"},
         "result_preview": "refused by user: write to a.md"},
        {"name": "fs_read", "args": {"path": "b.py"},
         "result_preview": "ok"},
        {"name": "shell", "args": {"command": "rm a"},
         "result_preview": "refused by user: shell command"},
    ]
    refs = memory_refusal.extract_refusals(trace)
    assert [r.tool for r in refs] == ["fs_write", "shell"]


def test_extract_skips_non_dict_entries():
    """Trace can contain odd shapes from buggy hooks; never crash."""
    from janus import memory_refusal
    trace = [
        None, "string entry", 42,
        {"name": "fs_write", "args": {"path": "x"},
         "result_preview": "refused by user: write to x"},
    ]
    refs = memory_refusal.extract_refusals(trace)  # type: ignore[arg-type]
    assert len(refs) == 1


def test_extract_falls_back_to_args_when_no_known_target_key():
    """Tool with unfamiliar arg shape — record args dict so signal isn't lost."""
    from janus import memory_refusal
    trace = [{
        "name": "weird_tool",
        "args": {"weird_key": "weird_value"},
        "result_preview": "refused by user: weird action",
    }]
    refs = memory_refusal.extract_refusals(trace)
    assert len(refs) == 1
    assert "weird" in refs[0].target


# ---------- synthesize_cards ----------


def test_synthesize_creates_constraint_card():
    from janus import memory_refusal
    trace = [{
        "name": "fs_write", "args": {"path": "docs/SWARM_EXPLAINER.md"},
        "result_preview": "refused by user: write to docs/SWARM_EXPLAINER.md",
    }]
    cards = memory_refusal.cards_from_trace(trace, current_scope="cli")
    assert len(cards) == 1
    c = cards[0]
    assert c.type == "constraint"
    assert c.origin_kind == "user_refusal"
    assert c.confidence >= 0.9
    assert c.durability >= 0.5
    assert "docs/SWARM_EXPLAINER.md" in c.content


def test_synthesize_subject_is_stable_for_same_target():
    """Re-refusing the same path should produce the same subject so
    the cards layer dedupes / appends meaningfully."""
    from janus import memory_refusal
    trace_a = [{
        "name": "fs_write", "args": {"path": "docs/X.md"},
        "result_preview": "refused by user: write to docs/X.md",
    }]
    trace_b = [{
        "name": "fs_write", "args": {"path": "docs/X.md"},
        "result_preview": "refused by user: write to docs/X.md",
    }]
    a = memory_refusal.cards_from_trace(trace_a)[0]
    b = memory_refusal.cards_from_trace(trace_b)[0]
    assert a.subject == b.subject


def test_synthesize_dedupes_within_one_turn():
    """If the model retries the same refused action twice in a turn,
    one card is enough."""
    from janus import memory_refusal
    trace = [
        {"name": "fs_write", "args": {"path": "docs/X.md"},
         "result_preview": "refused by user: write to docs/X.md"},
        {"name": "fs_write", "args": {"path": "docs/X.md"},
         "result_preview": "refused by user: write to docs/X.md"},
    ]
    cards = memory_refusal.cards_from_trace(trace)
    assert len(cards) == 1


def test_synthesize_fs_write_scope_is_global():
    """fs_write/fs_edit refusals apply across gateways — Sam's 'no docs'
    veto in CLI should also stop the Telegram bot from suggesting it."""
    from janus import memory_refusal
    trace = [{
        "name": "fs_write", "args": {"path": "docs/X.md"},
        "result_preview": "refused by user: write to docs/X.md",
    }]
    cards = memory_refusal.cards_from_trace(trace, current_scope="telegram:42")
    assert cards[0].scope == "global"


def test_synthesize_shell_refusal_keeps_local_scope():
    """A one-off shell refusal is per-context — don't promote globally."""
    from janus import memory_refusal
    trace = [{
        "name": "shell", "args": {"command": "rm /tmp/x"},
        "result_preview": "refused by user: shell command",
    }]
    cards = memory_refusal.cards_from_trace(trace, current_scope="telegram:42")
    assert cards[0].scope == "telegram:42"


def test_synthesize_empty_trace_returns_empty_list():
    from janus import memory_refusal
    assert memory_refusal.cards_from_trace([]) == []
    assert memory_refusal.cards_from_trace([
        {"name": "fs_read", "args": {}, "result_preview": "ok"}
    ]) == []


# ---------- cli_rich + cli wiring (source-level pin) ----------


def test_cli_rich_passes_trace_to_propose_memory():
    """Source-level pin: cli_rich._maybe_propose_memory must accept a
    `trace` kwarg and the call site must pass it."""
    import inspect
    from janus import cli_rich
    sig = inspect.signature(cli_rich._maybe_propose_memory)
    assert "trace" in sig.parameters
    src = inspect.getsource(cli_rich)
    assert "trace=trace" in src


def test_cli_basic_passes_trace_to_propose_memory():
    import inspect
    from janus import cli
    sig = inspect.signature(cli.maybe_propose_memory)
    assert "trace" in sig.parameters
    src = inspect.getsource(cli)
    assert "trace=trace" in src
