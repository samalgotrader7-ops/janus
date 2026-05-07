"""Tests for v1.28.0 — self-improving skills (Phase 4 #1).

Pattern detection (pure-compute) + LLM-gated drafting (opt-in via
slash command). The Janus differentiator: "Claude Code's UX with a
learning loop." v1.28.0 lights up the auto-proposal half of skills.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import config, skill_proposer
from janus.skill_proposer import (
    Pattern,
    detect,
    list_offerable,
    filter_offerable,
    is_in_cooldown,
    mark_offered,
    mark_declined,
    mark_accepted,
    format_offer_line,
    SEQ_MIN_OCCURRENCES,
    FILE_MIN_OCCURRENCES,
    SHAPE_MIN_OCCURRENCES,
    COOLDOWN_DAYS,
    _detect_repeated_sequences,
    _detect_repeated_files,
    _detect_repeated_shapes,
    _extract_tool_calls,
    _slug,
    _seq_pattern_id,
    _file_pattern_id,
)


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME / SKILLS_DIR / LOG_FILE at a fresh tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    skills_dir = home / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()
    return home


def _trace_calls(*pairs: tuple[str, str]) -> list[dict]:
    """Build a synthetic trace from (tool, path) tuples."""
    return [
        {"type": "tool_call", "tool": t, "args": {"path": p}}
        for t, p in pairs
    ]


# ============================================================
# Trace extraction
# ============================================================


def test_extract_tool_calls_picks_tool_call_type_only():
    trace = [
        {"type": "tool_call", "tool": "fs_read", "args": {"path": "a.py"}},
        {"type": "tool_result", "tool": "fs_read", "result_preview": "..."},
        {"type": "model_start"},
        {"type": "tool_call", "tool": "fs_edit", "args": {"path": "a.py"}},
    ]
    out = _extract_tool_calls(trace)
    assert len(out) == 2
    assert out[0]["tool"] == "fs_read"
    assert out[1]["tool"] == "fs_edit"


def test_extract_tool_calls_handles_empty_or_none():
    assert _extract_tool_calls(None) == []
    assert _extract_tool_calls([]) == []


def test_extract_tool_calls_pulls_path_from_args():
    trace = [{"type": "tool_call", "tool": "shell", "args": {"command": "ls"}}]
    out = _extract_tool_calls(trace)
    assert out[0]["path"] == "ls"


def test_extract_tool_calls_skips_malformed_entries():
    trace = [
        {"type": "tool_call", "tool": "fs_read"},  # no args, OK
        "not a dict",
        None,
        {"type": "tool_call"},  # no tool name
    ]
    out = _extract_tool_calls(trace)
    assert len(out) == 1


# ============================================================
# Sequence detection
# ============================================================


def test_seq_detection_finds_repeated_pair():
    calls = [
        {"tool": "fs_read", "path": ""},
        {"tool": "fs_edit", "path": ""},
        {"tool": "fs_read", "path": ""},
        {"tool": "fs_edit", "path": ""},
        {"tool": "fs_read", "path": ""},
        {"tool": "fs_edit", "path": ""},
    ]
    patterns = _detect_repeated_sequences(calls)
    # (fs_read, fs_edit) appears 3 times
    pair_pat = next(
        (p for p in patterns if p.detail.get("sequence") == ["fs_read", "fs_edit"]),
        None,
    )
    assert pair_pat is not None
    assert pair_pat.occurrences == 3


def test_seq_detection_below_threshold_returns_empty():
    calls = [
        {"tool": "fs_read", "path": ""},
        {"tool": "fs_edit", "path": ""},
        {"tool": "fs_read", "path": ""},
        {"tool": "fs_edit", "path": ""},
    ]
    # Pair appears 2 times, below SEQ_MIN_OCCURRENCES=3
    patterns = _detect_repeated_sequences(calls)
    assert patterns == []


def test_seq_detection_finds_three_tool_sequence():
    seq = [
        {"tool": "fs_read", "path": ""},
        {"tool": "fs_edit", "path": ""},
        {"tool": "shell", "path": ""},
    ]
    calls = seq * 3
    patterns = _detect_repeated_sequences(calls)
    triple = next(
        (p for p in patterns if p.detail.get("length") == 3),
        None,
    )
    assert triple is not None
    assert triple.detail["sequence"] == ["fs_read", "fs_edit", "shell"]


def test_seq_detection_dedupe_returns_unique_ids():
    """Same sequence at multiple lengths should not produce duplicate ids."""
    seq = [{"tool": "fs_read", "path": ""}, {"tool": "fs_edit", "path": ""}]
    calls = seq * 4
    patterns = _detect_repeated_sequences(calls)
    ids = [p.id for p in patterns]
    assert len(ids) == len(set(ids))


# ============================================================
# Repeated-file detection
# ============================================================


def test_file_detection_finds_repeated_file():
    calls = [{"tool": "fs_read", "path": "src/foo.py"}] * 4
    patterns = _detect_repeated_files(calls)
    foo_pat = next(
        (p for p in patterns if p.detail.get("path") == "src/foo.py"),
        None,
    )
    assert foo_pat is not None
    assert foo_pat.occurrences == 4


def test_file_detection_below_threshold_skips():
    calls = [{"tool": "fs_read", "path": "foo.py"}] * 3  # < 4
    patterns = _detect_repeated_files(calls)
    assert patterns == []


def test_file_detection_skips_urls():
    calls = [
        {"tool": "web_fetch", "path": "https://example.com/a"},
    ] * 4
    patterns = _detect_repeated_files(calls)
    assert patterns == []


def test_file_detection_skips_shell_commands():
    calls = [
        {"tool": "shell", "path": "git status"},  # space → command
    ] * 4
    patterns = _detect_repeated_files(calls)
    assert patterns == []


def test_file_detection_sorts_by_count_descending():
    calls = (
        [{"tool": "fs_read", "path": "lots.py"}] * 8
        + [{"tool": "fs_read", "path": "few.py"}] * 4
    )
    patterns = _detect_repeated_files(calls)
    assert patterns[0].detail["path"] == "lots.py"


# ============================================================
# Shape detection
# ============================================================


def test_shape_detection_finds_read_edit_exec_shape():
    """[fs_read fs_edit shell] across different files = same shape."""
    calls = [
        {"tool": "fs_read", "path": "a.py"},
        {"tool": "fs_edit", "path": "a.py"},
        {"tool": "shell", "path": "pytest"},
        {"tool": "fs_read", "path": "b.py"},
        {"tool": "fs_edit", "path": "b.py"},
        {"tool": "shell", "path": "pytest"},
        {"tool": "fs_read", "path": "c.py"},
        {"tool": "fs_edit", "path": "c.py"},
        {"tool": "shell", "path": "pytest"},
    ]
    patterns = _detect_repeated_shapes(calls)
    shape_pat = next(
        (p for p in patterns
         if p.detail.get("shape") == ["read", "edit", "exec"]),
        None,
    )
    assert shape_pat is not None
    assert shape_pat.occurrences == 3


def test_shape_detection_skips_degenerate_all_same_class():
    """Shape [read read read] is degenerate (all-same) — should not
    propose it as a pattern."""
    calls = [{"tool": "fs_read", "path": f"f{i}.py"} for i in range(10)]
    patterns = _detect_repeated_shapes(calls)
    # No shape with all-same class allowed
    for p in patterns:
        shape = p.detail.get("shape", [])
        assert len(set(shape)) >= 2


def test_shape_detection_below_threshold_returns_empty():
    calls = [
        {"tool": "fs_read", "path": "a.py"},
        {"tool": "fs_edit", "path": "a.py"},
    ]
    patterns = _detect_repeated_shapes(calls)
    assert patterns == []


# ============================================================
# Top-level detect()
# ============================================================


def test_detect_combines_all_pattern_types(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    seq = [
        {"type": "tool_call", "tool": "fs_read", "args": {"path": "x.py"}},
        {"type": "tool_call", "tool": "fs_edit", "args": {"path": "x.py"}},
        {"type": "tool_call", "tool": "shell", "args": {"command": "pytest"}},
    ]
    trace = seq * 3
    patterns = detect(current_trace=trace)
    kinds = {p.kind for p in patterns}
    assert "repeated_tool_sequence" in kinds
    assert "repeated_file" in kinds
    assert "repeated_shape" in kinds


def test_detect_empty_trace_returns_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert detect(current_trace=[]) == []
    assert detect(current_trace=None) == []


def test_detect_sorts_by_occurrences_desc(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # File "many.py" gets touched 8 times vs sequences with 3 occurrences
    trace = (
        [
            {"type": "tool_call", "tool": "fs_read", "args": {"path": "many.py"}}
        ] * 8
        + [
            {"type": "tool_call", "tool": "fs_read", "args": {"path": "x.py"}},
            {"type": "tool_call", "tool": "fs_edit", "args": {"path": "x.py"}},
        ] * 3
    )
    patterns = detect(current_trace=trace)
    assert patterns[0].occurrences >= 8


# ============================================================
# Cooldown / state persistence
# ============================================================


def test_mark_offered_writes_state(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mark_offered("test-pattern-1")
    state_path = config.SKILLS_DIR / "_proposals_state.json"
    assert state_path.exists()
    import json
    state = json.loads(state_path.read_text())
    assert "test-pattern-1" in state
    assert state["test-pattern-1"].get("last_offered")


def test_is_in_cooldown_freshly_offered_returns_true(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mark_offered("p")
    assert is_in_cooldown("p") is True


def test_is_in_cooldown_unknown_pattern_returns_false(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert is_in_cooldown("never-seen") is False


def test_is_in_cooldown_old_offer_returns_false(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # Hand-craft a state file with an old timestamp
    import json
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS + 1)).isoformat()
    (config.SKILLS_DIR / "_proposals_state.json").write_text(
        json.dumps({"old-p": {"last_offered": old}}),
        encoding="utf-8",
    )
    assert is_in_cooldown("old-p") is False


def test_mark_declined_records_decline_count(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mark_declined("p")
    mark_declined("p")
    import json
    state = json.loads(
        (config.SKILLS_DIR / "_proposals_state.json").read_text()
    )
    assert state["p"]["decline_count"] == 2


def test_filter_offerable_drops_in_cooldown(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    p1 = Pattern(id="p1", kind="x", description="d1", occurrences=5)
    p2 = Pattern(id="p2", kind="x", description="d2", occurrences=5)
    mark_offered("p1")
    out = filter_offerable([p1, p2])
    ids = {p.id for p in out}
    assert ids == {"p2"}


def test_filter_offerable_drops_accepted(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    p1 = Pattern(id="p1", kind="x", description="d", occurrences=5)
    mark_accepted("p1")
    out = filter_offerable([p1])
    assert out == []


# ============================================================
# Helper functions
# ============================================================


def test_slug_lowercases_and_dashes():
    assert _slug("Some File Path/x.py") == "some-file-path-x-py"


def test_slug_truncates_long():
    long = "x" * 200
    assert len(_slug(long, maxlen=50)) <= 50


def test_seq_pattern_id_stable():
    """Same sequence always gets the same id (so cooldown survives)."""
    a = _seq_pattern_id(("fs_read", "fs_edit"))
    b = _seq_pattern_id(("fs_read", "fs_edit"))
    assert a == b


def test_file_pattern_id_includes_path_slug():
    pid = _file_pattern_id("src/foo.py")
    assert "foo" in pid


def test_format_offer_line_includes_propose_command():
    p = Pattern(id="p1", kind="x", description="hello world", occurrences=3)
    line = format_offer_line(p)
    assert "propose p1" in line
    assert "hello world" in line


# ============================================================
# History from log.jsonl
# ============================================================


def test_detect_includes_log_history(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # Write 4 log entries each with the same 2-tool sequence
    import json
    log = config.LOG_FILE
    for i in range(4):
        log.open("a", encoding="utf-8").write(
            json.dumps({
                "type": "turn",
                "trace": [
                    {"type": "tool_call", "tool": "fs_read", "args": {"path": "x.py"}},
                    {"type": "tool_call", "tool": "fs_edit", "args": {"path": "x.py"}},
                ],
            }) + "\n"
        )
    # No current trace — should still detect from log history
    patterns = detect(current_trace=None)
    seq_pat = next(
        (p for p in patterns if p.detail.get("sequence") == ["fs_read", "fs_edit"]),
        None,
    )
    assert seq_pat is not None
    assert seq_pat.occurrences >= 3


def test_detect_handles_missing_log(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # No log file at all — should not crash
    patterns = detect(current_trace=None)
    assert patterns == []


# ============================================================
# Drafting (with mocked LLM)
# ============================================================


def test_draft_skill_writes_quarantined_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # Stub the LLM call to return a deterministic draft
    fake_draft = {
        "name": "auto-fix-loop",
        "description": "edit + test + repeat",
        "body": "# steps\n1. fs_read\n2. fs_edit\n3. shell pytest",
        "capabilities": {},
    }
    monkeypatch.setattr(
        "janus.skills.draft_skill_from_log",
        lambda *a, **kw: fake_draft,
    )
    p = Pattern(
        id="seq-fs-read-fs-edit-shell",
        kind="repeated_tool_sequence",
        description="fs_read → fs_edit → shell",
        occurrences=3,
        detail={"sequence": ["fs_read", "fs_edit", "shell"], "length": 3},
    )
    trace = [
        {"type": "tool_call", "tool": "fs_read", "args": {"path": "x.py"}},
        {"type": "tool_call", "tool": "fs_edit", "args": {"path": "x.py"}},
        {"type": "tool_call", "tool": "shell", "args": {"command": "pytest"}},
    ] * 3
    path = skill_proposer.draft_skill(p, current_trace=trace)
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert "auto-fix-loop" in body
    assert "state: quarantined" in body
    # Acceptance is recorded so it doesn't get re-offered
    assert not is_in_cooldown(p.id)  # cooldown != accepted; check accepted state
    import json
    state = json.loads(
        (config.SKILLS_DIR / "_proposals_state.json").read_text()
    )
    assert state[p.id].get("accepted_at")


def test_draft_skill_falls_back_when_llm_returns_garbage(tmp_path, monkeypatch):
    """If LLM returns empty / non-dict, fabricate a minimal stub
    rather than crashing."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "janus.skills.draft_skill_from_log",
        lambda *a, **kw: {},  # empty
    )
    p = Pattern(
        id="seq-x",
        kind="repeated_tool_sequence",
        description="test pattern",
        occurrences=3,
        detail={"sequence": ["a", "b"]},
    )
    path = skill_proposer.draft_skill(p, current_trace=[])
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    # Stub uses pattern.id as name when LLM fails
    assert "seq-x" in body or "test pattern" in body


# ============================================================
# CLI integration source-pins
# ============================================================


def test_cli_rich_skills_dispatcher_routes_suggestions():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "suggestions" in src
    assert "_cmd_skills_suggestions" in src


def test_cli_rich_skills_dispatcher_routes_propose():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "_cmd_skills_propose" in src


def test_cli_rich_skills_dispatcher_routes_decline():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "_cmd_skills_decline" in src


def test_cli_rich_handlers_exist():
    from janus import cli_rich
    assert hasattr(cli_rich, "_cmd_skills_suggestions")
    assert hasattr(cli_rich, "_cmd_skills_propose")
    assert hasattr(cli_rich, "_cmd_skills_decline")
