"""Tests for v1.24.6 anti-patterns: source-spelunking on explanation
questions + writes to docs/ without permission.

From Sam's 2026-05-07 7:26 AM session:
  User: "Janus, please explain it how your agent swarms is working
         and give me an example"
  Janus: did 8+ fs_read/fs_grep/fs_list/shell calls reading
         docs/JANUS_MASTER_SPEC.md, swarms/runner.py, spec.py,
         aggregators.py, ~/.janus/swarms/specs/, etc. — 5+ minutes
         of waiting before any answer. Then proposed writing to
         docs/SWARM_EXPLAINER.md (refused by user).

This file pins:
  * Rule 22 — explanation questions answer from injected context.
  * Rule 23 — docs/ and other project-owned dirs aren't agent-writable
              without explicit user permission.
  * tool_guardrails.check warns on fs_write / fs_edit targeting docs/.
"""
from __future__ import annotations


# ---------- Rule 22: don't spelunk source for explanation Qs ----------


def test_rule_22_explanation_anti_pattern_in_prompt():
    from janus.executor import JANUS_CHAT_SYSTEM
    assert "EXPLANATION QUESTIONS" in JANUS_CHAT_SYSTEM
    # The rule MUST name the wrong-shape behavior the model showed.
    assert "fs_read" in JANUS_CHAT_SYSTEM
    assert "fs_grep" in JANUS_CHAT_SYSTEM
    # And direct the right-shape behavior (read injected context).
    assert "INJECTED" in JANUS_CHAT_SYSTEM
    assert "CLAUDE.md" in JANUS_CHAT_SYSTEM


def test_rule_22_quotes_sams_2026_05_07_session():
    """Future readers should be able to grep 'swarm' in the prompt
    and find the worked example documenting why the rule exists."""
    from janus.executor import JANUS_CHAT_SYSTEM
    assert "swarm" in JANUS_CHAT_SYSTEM.lower()
    assert "5+ minutes" in JANUS_CHAT_SYSTEM or "5 minutes" in JANUS_CHAT_SYSTEM


def test_rule_22_distinguishes_explanation_from_code_change():
    """The rule should preserve the case where source-reading IS
    appropriate — concrete code-change tasks. Otherwise the model
    will over-correct and refuse to read source even when asked
    to fix a bug."""
    from janus.executor import JANUS_CHAT_SYSTEM
    # The phrasing names the concrete-code-change carve-out.
    assert "code-change" in JANUS_CHAT_SYSTEM.lower() or \
           "fix the bug" in JANUS_CHAT_SYSTEM


# ---------- Rule 23: don't write to docs/ without asking ----------


def test_rule_23_docs_protection_in_prompt():
    from janus.executor import JANUS_CHAT_SYSTEM
    assert "docs/" in JANUS_CHAT_SYSTEM
    assert "owned by the user" in JANUS_CHAT_SYSTEM


def test_rule_23_lists_other_protected_dirs():
    from janus.executor import JANUS_CHAT_SYSTEM
    # The rule should call out at least the common ones so the model
    # generalizes beyond just docs/.
    for needle in ("docs/", ".github/", "vendor/"):
        assert needle in JANUS_CHAT_SYSTEM, f"{needle!r} missing from rule 23"


def test_rule_23_quotes_sams_swarm_explainer_refusal():
    from janus.executor import JANUS_CHAT_SYSTEM
    # The worked example references the actual file Sam refused.
    assert "SWARM_EXPLAINER" in JANUS_CHAT_SYSTEM


def test_explanation_and_docs_anchors_present_in_correct_sections():
    """v1.26.0 dropped numbered rules (1-23) in favor of 6 grouped
    sections. The explanation-questions rule lives in section 3
    (Memory) — both anti-patterns are about content already in your
    context. The protected-dirs rule lives in section 5 (Mode)
    alongside permission-mode behavior. Pin the section placement so
    future edits don't accidentally relocate them."""
    from janus.executor import JANUS_CHAT_SYSTEM
    s = JANUS_CHAT_SYSTEM
    # Section anchors
    sec3 = s.find("# 3. Memory")
    sec4 = s.find("# 4. Verification")
    sec5 = s.find("# 5. Mode")
    sec6 = s.find("# 6. Errors")
    assert -1 < sec3 < sec4 < sec5 < sec6, "section ordering broken"
    # EXPLANATION QUESTIONS lives between section 3 and section 4
    expl_pos = s.find("EXPLANATION QUESTIONS")
    assert sec3 < expl_pos < sec4, (
        "EXPLANATION QUESTIONS rule must live in section 3 (Memory)"
    )
    # docs/ owned-by-user rule lives between section 5 and section 6
    docs_pos = s.find("owned by the user")
    assert sec5 < docs_pos < sec6, (
        "Protected-dirs rule must live in section 5 (Mode)"
    )


# ---------- tool_guardrails: docs/ write warning ----------


def test_guardrail_warns_on_fs_write_to_docs(tmp_path, monkeypatch):
    """fs_write targeting <repo>/docs/<anything>.md should surface a
    'writing to docs/' warning at the guardrail layer, so the user
    sees a yellow flag at approval time even if the prompt-rule
    didn't dissuade the model."""
    from janus import tool_guardrails
    repo = tmp_path / "fakerepo"
    (repo / "docs").mkdir(parents=True)
    target = repo / "docs" / "SWARM_EXPLAINER.md"
    warning = tool_guardrails.check(
        "fs_write",
        {"path": str(target), "content": "# explainer\n"},
    )
    assert warning != ""
    assert "[guardrail]" in warning
    assert "docs/" in warning


def test_guardrail_warns_on_fs_edit_to_docs(tmp_path):
    from janus import tool_guardrails
    repo = tmp_path / "fakerepo"
    (repo / "docs").mkdir(parents=True)
    target = repo / "docs" / "ARCHITECTURE.md"
    target.write_text("# arch\n", encoding="utf-8")
    warning = tool_guardrails.check(
        "fs_edit",
        {"path": str(target), "old_string": "arch", "new_string": "design"},
    )
    assert warning != ""
    assert "docs/" in warning


def test_guardrail_no_docs_warn_for_non_docs_write(tmp_path):
    from janus import tool_guardrails
    target = tmp_path / "src" / "foo.py"
    target.parent.mkdir(parents=True)
    warning = tool_guardrails.check(
        "fs_write",
        {"path": str(target), "content": "x = 1\n"},
    )
    # No docs/ in the path → no docs/ warning. (Other warnings may
    # still appear from existing checks; we just want to confirm the
    # docs/ check doesn't false-positive.)
    assert "docs/" not in warning


def test_guardrail_warns_on_other_protected_paths(tmp_path):
    """The protected-path catalogue should also flag .github/ and
    CHANGELOG.md / LICENSE / vendor/ writes as borderline."""
    from janus import tool_guardrails
    cases = [
        tmp_path / ".github" / "workflows" / "ci.yml",
        tmp_path / "CHANGELOG.md",
        tmp_path / "LICENSE",
        tmp_path / "vendor" / "lib.js",
    ]
    for target in cases:
        target.parent.mkdir(parents=True, exist_ok=True)
        warning = tool_guardrails.check(
            "fs_write",
            {"path": str(target), "content": "x"},
        )
        assert warning != "", f"expected guardrail for {target}"
        assert "[guardrail]" in warning


def test_guardrail_docs_check_does_not_crash_on_relative_path():
    """check() must never raise; relative paths should still trigger
    the docs/ pattern match without exploding."""
    from janus import tool_guardrails
    out = tool_guardrails.check(
        "fs_write",
        {"path": "docs/notes.md", "content": "x"},
    )
    assert "[guardrail]" in out
    assert "docs/" in out
