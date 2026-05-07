"""Tests for v1.26.0 — system prompt rewrite (Phase 3 priority #1).

The pre-v1.26 ``JANUS_CHAT_SYSTEM`` had grown to 23 ad-hoc numbered
rules across 9 unstructured sections — duplicative, sprawling, and
hard to extend. v1.26.0 restructures it into 6 grouped sections:

  1. Tone
  2. Tool selection
  3. Memory
  4. Verification
  5. Mode
  6. Errors

Plus the existing ``# Janus configuration surface`` reference appendix.

These tests pin the new structural invariants so future edits can't
silently regress to the old shape. Behavioral pins (does the prompt
still mention fs_write? memory_search? agent_create?) are spread
across the existing test_*.py files; this file pins STRUCTURE only.
"""
from __future__ import annotations

import re

from janus import executor


# ---------- Six section headers ----------


SECTION_HEADERS = (
    "# 1. Tone",
    "# 2. Tool selection",
    "# 3. Memory",
    "# 4. Verification",
    "# 5. Mode",
    "# 6. Errors",
)


def test_all_six_section_headers_present():
    """Every section must exist by exact header."""
    s = executor.JANUS_CHAT_SYSTEM
    for header in SECTION_HEADERS:
        assert header in s, f"missing section header: {header!r}"


def test_section_headers_appear_in_order():
    """Section ordering is part of the contract — tone first, errors
    last. Reordering changes the model's read order and is a
    behavior-affecting change that should be deliberate."""
    s = executor.JANUS_CHAT_SYSTEM
    positions = [s.find(h) for h in SECTION_HEADERS]
    assert all(p > -1 for p in positions), "header missing"
    assert positions == sorted(positions), (
        f"sections out of order: {list(zip(SECTION_HEADERS, positions))}"
    )


def test_config_surface_appendix_after_section_6():
    """The ~/.janus/ reference block lives after the 6 numbered
    sections, not interleaved with them."""
    s = executor.JANUS_CHAT_SYSTEM
    sec6 = s.find("# 6. Errors")
    appendix = s.find("# Janus configuration surface")
    assert sec6 > -1 and appendix > sec6, (
        "config surface appendix must come after section 6"
    )


# ---------- Old structural markers removed ----------


def test_no_legacy_RULES_header():
    """The old `# RULES` umbrella section is gone — rules are grouped
    by theme now, not piled under one header."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "# RULES" not in s


def test_no_legacy_when_chat_appropriate_section():
    """Folded into section 1 (Tone) as a bullet — no separate header."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "# WHEN CHAT IS APPROPRIATE" not in s


def test_no_legacy_multistep_tasks_section():
    """Folded into section 2 (Tool selection) as a bullet."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "# MULTI-STEP TASKS" not in s


def test_no_legacy_coding_agent_conventions_section():
    """Coding-agent rules merged into sections 1-2 by theme."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "# CODING-AGENT CONVENTIONS" not in s


def test_no_legacy_memory_anti_pattern_section():
    """The standalone "MEMORY — IT'S ALREADY IN YOUR CONTEXT" header
    is now just section 3."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "# MEMORY — IT'S ALREADY" not in s


def test_no_legacy_explanation_questions_section_header():
    """The phrase EXPLANATION QUESTIONS still appears (as anchor for
    the rule) but its old standalone section header is gone."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "# EXPLANATION QUESTIONS" not in s
    # The phrase itself must still appear (kept as section 3 anchor)
    assert "EXPLANATION QUESTIONS" in s


def test_no_legacy_docs_section_header():
    """The standalone "DOCS/ AND OTHER PROJECT-OWNED DIRS" header is
    gone — the rule is a bullet in section 5 (Mode)."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "# DOCS/" not in s


def test_no_legacy_2b_marker():
    """Sub-numbered rules ("2b") are gone."""
    s = executor.JANUS_CHAT_SYSTEM
    # The literal "2b." or "2b**" markers used in pre-v1.26 numbering
    # should not appear. We do a narrow check rather than `"2b" in s`
    # because "2b" could legitimately match a future config example.
    assert "2b. **" not in s
    assert "2b**" not in s


def test_no_numbered_rules_1_through_23():
    """v1.26.0 dropped per-rule numbering. The old shape was
    ``1. **When the user...``, ``2. **When...``, etc. None should
    survive. Bullets (`- `) are the new shape."""
    s = executor.JANUS_CHAT_SYSTEM
    # Pattern: digit(s) + ". **" near a line start. Allow up to 4
    # leading whitespace chars (continuation of indented bullet).
    pattern = re.compile(r"(?:^|\n)\s{0,4}(\d{1,2})\. \*\*")
    matches = pattern.findall(s)
    # Any match means a numbered rule survived. Section headers are
    # ``# 1. Tone`` etc — those have ``# `` before the digit, which
    # the pattern excludes via the leading whitespace constraint
    # AND the absence of `# ` in the captured group.
    # Filter out section headers explicitly (defense in depth):
    filtered = [m for m in matches if int(m) > 6]
    assert filtered == [], (
        f"numbered-rule markers survived: {filtered}"
    )


# ---------- Anti-duplication pins ----------


def test_default_to_act_appears_exactly_once():
    """Pre-v1.26 had this rule in two places (rule 7 mode bias + the
    'WHEN CHAT IS APPROPRIATE' carve-out). v1.26 keeps it ONCE in
    section 5 (Mode)."""
    s = executor.JANUS_CHAT_SYSTEM
    occurrences = s.count("default to ACT")
    assert occurrences == 1, (
        f"'default to ACT' appears {occurrences} times — should be 1"
    )


def test_memory_is_injected_appears_exactly_once():
    """Pre-v1.26 the "memory is injected" idea spanned rule 18
    explicitly + reinforcing language in rule 22. v1.26 has the
    sentinel phrase exactly once in section 3."""
    s = executor.JANUS_CHAT_SYSTEM
    occurrences = s.count("Memory is INJECTED")
    assert occurrences == 1, (
        f"'Memory is INJECTED' appears {occurrences} times — should be 1"
    )


# ---------- Length budget (modest reduction is the goal) ----------


def test_prompt_within_length_budget():
    """v1.26.0's bullet-style sections render bigger on disk than the
    pre-v1.26 ``\\``-continuation-packed paragraphs (same content, more
    whitespace). The CAP is set to catch accidental DOUBLING of a
    section (a future edit that forgot to delete the old version),
    not to enforce a rigid byte budget. If a future release deliberately
    adds bullets and bumps past 18 KB, raise this cap consciously."""
    s = executor.JANUS_CHAT_SYSTEM
    assert len(s) <= 18000, (
        f"prompt is {len(s)} chars — over the 18 KB ceiling. "
        "Investigate before raising: did a section get duplicated?"
    )


def test_prompt_not_pathologically_short():
    """Lower bound — defense against an empty/truncated prompt."""
    s = executor.JANUS_CHAT_SYSTEM
    assert len(s) >= 8000, (
        f"prompt is only {len(s)} chars — likely truncated."
    )


# ---------- Prompt-injection note moved to section 6 ----------


def test_prompt_injection_warning_in_section_6():
    """Pre-v1.26 the prompt-injection note lived ONLY in the
    auto-mode appendage (added by ``_build_chat_system``). v1.26
    moves it into section 6 (Errors) of the main prompt so it
    applies in every mode, not just auto."""
    s = executor.JANUS_CHAT_SYSTEM
    sec6 = s.find("# 6. Errors")
    appendix = s.find("# Janus configuration surface")
    assert sec6 > -1 and appendix > sec6
    sec6_body = s[sec6:appendix]
    assert "Prompt injection" in sec6_body or "prompt injection" in sec6_body.lower()
    assert "obey" in sec6_body.lower()


def test_auto_mode_appendage_no_longer_duplicates_injection_note(tmp_path):
    """Now that section 6 carries the prompt-injection rule, the
    auto-mode appendage doesn't repeat it. Keeps the auto-mode block
    focused on auto-specific behavior (safety analyzer + refusal-as-
    feedback)."""
    out = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="auto",
        memory_preamble="",
        skill_body="",
        tool_count=10,
        skill_count=10,
    )
    # Auto-mode block is appended after JANUS_CHAT_SYSTEM. Find that
    # boundary — everything after the section-6 body up to "Workspace:"
    # plus the trailing mode block is the auto-mode-specific area.
    idx = out.find("AUTO mode")
    assert idx >= 0, "auto-mode block missing from assembled prompt"
    # Only count occurrences of the injection-warning phrase AFTER the
    # AUTO header — should be 0 now (it's in section 6 BEFORE).
    after_auto = out[idx:]
    assert "Prompt-injection content" not in after_auto, (
        "auto-mode block should not duplicate the injection note"
    )


# ---------- Round-trip: assembled prompt has every section ----------


def test_assembled_prompt_includes_all_sections_in_default_mode(tmp_path):
    out = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="default",
        memory_preamble="",
        skill_body="",
        tool_count=10,
        skill_count=10,
    )
    for header in SECTION_HEADERS:
        assert header in out, f"section {header!r} missing from default-mode prompt"


def test_assembled_prompt_includes_all_sections_in_auto_mode(tmp_path):
    out = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="auto",
        memory_preamble="",
        skill_body="",
        tool_count=10,
        skill_count=10,
    )
    for header in SECTION_HEADERS:
        assert header in out, f"section {header!r} missing from auto-mode prompt"
    # Mode-specific suffix still present
    assert "AUTO mode" in out


def test_assembled_prompt_includes_all_sections_in_plan_mode(tmp_path):
    out = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="plan",
        memory_preamble="",
        skill_body="",
        tool_count=10,
        skill_count=10,
    )
    for header in SECTION_HEADERS:
        assert header in out, f"section {header!r} missing from plan-mode prompt"
    assert "PLAN mode" in out
