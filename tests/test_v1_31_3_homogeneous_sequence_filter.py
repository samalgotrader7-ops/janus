"""Tests for v1.31.3 — homogeneous sequence filter in skill_proposer.

FIELD-VALIDATION REPORT (Sam, 2026-05-07-evening, v1.31.2 VPS run):

  > Tool sequence 'shell → shell → shell → shell' appeared 77 times.
  > /skills propose seq-shell-shell-shell-shell to draft, ...
  > is this normal

No, it's a bug. The shape detector already filters degenerate
shapes (``len(set(shape)) < 2``) but the sequence detector didn't,
so a single shell-heavy session would surface a useless skill
suggestion. v1.31.3 mirrors the same filter into
``_detect_repeated_sequences``.

DESIGN INVARIANT PINNED:
  * Same-tool-N-times sequences are never patterns. They don't
    generalize to a learnable skill body — "the user used shell
    a lot" doesn't tell you what they were doing.
"""

from __future__ import annotations

from janus import skill_proposer


def _calls(*tool_names: str) -> list[dict]:
    """Build a fake call list with empty paths."""
    return [{"tool": t, "path": ""} for t in tool_names]


# ============================================================
# The exact field report
# ============================================================


def test_77x_shell_run_does_not_propose_homogeneous_sequence():
    """Pin the exact shape Sam saw on his VPS: 77 shell calls in
    a row produce ZERO repeated_tool_sequence patterns. (May still
    produce a repeated_file pattern if a command repeats, but
    that's a separate signal — not the bug we're fixing here.)"""
    calls = _calls(*(["shell"] * 77))
    patterns = skill_proposer._detect_repeated_sequences(calls)
    seq_descriptions = [
        p.description for p in patterns
        if p.kind == "repeated_tool_sequence"
    ]
    assert seq_descriptions == [], (
        f"degenerate shell-only sequence leaked through filter: "
        f"{seq_descriptions}"
    )


def test_77x_shell_no_homogeneous_pattern_at_any_length():
    """Verify lengths 2/3/4 all skip the homogeneous tuple."""
    calls = _calls(*(["shell"] * 77))
    patterns = skill_proposer._detect_repeated_sequences(calls)
    for p in patterns:
        seq = p.detail.get("sequence") or []
        assert len(set(seq)) >= 2, (
            f"homogeneous sequence {seq} survived filter"
        )


# ============================================================
# Negative tests — heterogeneous sequences still detected
# ============================================================


def test_real_pattern_still_detected():
    """The fix must not regress the actual-useful case: a real
    workflow like ``fs_read → fs_edit → shell`` repeated 5 times
    should still surface."""
    sequence = ["fs_read", "fs_edit", "shell"]
    # Repeat the sequence 5 times with some noise between to be realistic
    calls = []
    for _ in range(5):
        calls.extend(_calls(*sequence))
    patterns = skill_proposer._detect_repeated_sequences(calls)
    # Find the targeted pattern
    matching = [
        p for p in patterns
        if p.detail.get("sequence") == sequence
    ]
    assert matching, (
        "real heterogeneous pattern was filtered out — false positive "
        "in the homogeneous filter. Patterns surfaced: "
        + str([p.detail.get("sequence") for p in patterns])
    )


def test_two_unique_tools_in_sequence_still_detected():
    """Edge of the filter: a 4-length sequence with TWO unique
    tools (e.g., fs_read, fs_read, fs_edit, fs_edit) should
    still surface — only ALL-SAME is filtered."""
    sequence = ["fs_read", "fs_read", "fs_edit", "fs_edit"]
    calls = []
    for _ in range(4):
        calls.extend(_calls(*sequence))
    patterns = skill_proposer._detect_repeated_sequences(calls)
    matching = [
        p for p in patterns
        if p.detail.get("sequence") == sequence
    ]
    assert matching, (
        "2-unique-tool sequence wrongly filtered — only "
        "ALL-SAME (1-unique) should be excluded"
    )


def test_mixed_workload_does_not_lose_heterogeneous_patterns():
    """Realistic mix: heavy shell use AND a real fs_read+fs_edit
    pattern. The shell-only homogeneous noise is filtered; the
    real fs_read+fs_edit pattern surfaces."""
    calls = []
    # 50 heavy shell calls (homogeneous noise)
    calls.extend(_calls(*(["shell"] * 50)))
    # 5 real workflow occurrences (heterogeneous)
    for _ in range(5):
        calls.extend(_calls("fs_read", "fs_edit"))
    patterns = skill_proposer._detect_repeated_sequences(calls)
    descriptions = {p.description for p in patterns}
    seqs = [p.detail.get("sequence") for p in patterns]
    # The real pattern must surface
    assert any(s == ["fs_read", "fs_edit"] for s in seqs), (
        f"real pattern lost in homogeneous noise: {seqs}"
    )
    # And no homogeneous shell pattern
    for p in patterns:
        seq = p.detail.get("sequence") or []
        assert len(set(seq)) >= 2, (
            f"homogeneous shell pattern leaked: {p.description}"
        )


# ============================================================
# detect() — top-level entry point regression
# ============================================================


def test_detect_top_level_excludes_homogeneous():
    """The bug Sam saw was via ``skill_proposer.list_offerable``
    which calls ``detect()`` which calls ``_detect_repeated_sequences``.
    Pin the top-level too to catch any future re-introduction
    via a different code path."""
    calls = _calls(*(["shell"] * 50))
    # Build a fake trace shaped like a real one
    trace = [
        {"type": "tool_call", "tool": c["tool"], "args": {}}
        for c in calls
    ]
    patterns = skill_proposer.detect(current_trace=trace)
    seq_patterns = [
        p for p in patterns
        if p.kind == "repeated_tool_sequence"
    ]
    for p in seq_patterns:
        seq = p.detail.get("sequence") or []
        assert len(set(seq)) >= 2, (
            f"detect() leaked homogeneous sequence: {p}"
        )


# ============================================================
# Source pin
# ============================================================


def test_source_has_v1_31_3_marker_and_filter():
    """Pin the marker comment so future refactors keep the filter
    explicit — the field-report context is preserved in source."""
    import inspect
    src = inspect.getsource(skill_proposer._detect_repeated_sequences)
    assert "v1.31.3" in src
    assert "len(set(seq))" in src


# ============================================================
# Symmetry with shape detector
# ============================================================


def test_shape_detector_already_had_filter():
    """Sanity: confirm the shape detector still has its own
    homogeneous filter (the one we're matching). If this regresses,
    the fix shifts to the same kind on shapes."""
    import inspect
    src = inspect.getsource(skill_proposer._detect_repeated_shapes)
    assert "len(set(shape))" in src
