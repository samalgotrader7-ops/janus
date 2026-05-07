"""Tests for v1.24.5: defer/auto-apply propose_diff when assistant
ends with a question.

Sam reported (2026-05-07): asked Janus to run pytest, Janus finished
and asked "Want me to install pytest-asyncio and re-run, or investigate
the web auth failure?". The propose_diff prompt fired immediately and
asked "apply? [y/N]". Sam typed "y" — but it went to the memory
prompt, not to Janus. Janus's question was lost; the next prompt was
empty. Memory got applied to project.md but the actual user-Janus
flow was broken.

Fix: when the assistant's reply ends with a user-directed question,
auto-apply ops+cards silently (no y/N prompt) so the user's next
input goes straight to the agent.
"""
from __future__ import annotations

import pytest


# ---------- _output_ends_with_question ----------


def test_question_marker_explicit_question_mark():
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    assert _output_ends_with_question("Should I proceed?") is True
    assert _output_ends_with_question("Done.\nDid that help?") is True


def test_question_marker_sams_exact_phrase():
    """Sam's screenshot — exact phrase that triggered the bug."""
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    text = (
        "**2267 passed, 16 failed, 2 skipped** in 67s.\n\n"
        "Want me to install `pytest-asyncio` and re-run, or "
        "investigate the web auth failure?"
    )
    assert _output_ends_with_question(text) is True


def test_question_marker_phrase_without_question_mark():
    """Some assistants emit 'Let me know if you want X' without ?"""
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    assert _output_ends_with_question(
        "Done. Let me know if you want me to refactor the helpers."
    ) is True


def test_question_marker_should_i_phrase():
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    assert _output_ends_with_question(
        "Plan looks good. Should I proceed with the migration"
    ) is True


def test_question_marker_negative_simple_done():
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    assert _output_ends_with_question("Done. wrote /tmp/x.md") is False


def test_question_marker_negative_status_report():
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    assert _output_ends_with_question(
        "Tests passed: 12/12. All green."
    ) is False


def test_question_marker_negative_future_tense_no_question():
    """Future-tense narration shouldn't trigger — that's the v1.17
    nudge territory."""
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    assert _output_ends_with_question("I'll do that now.") is False


def test_question_marker_negative_empty():
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    assert _output_ends_with_question("") is False
    assert _output_ends_with_question("   \n\n  ") is False


def test_question_marker_question_at_end_with_emoji():
    pytest.importorskip("rich")
    from janus.cli_rich import _output_ends_with_question
    text = "Want me to install pytest-asyncio? 🤔"
    assert _output_ends_with_question(text) is True


def test_question_marker_works_in_cli_basic_too():
    """Same heuristic ships in cli.py (basic ANSI surface)."""
    from janus.cli import _output_ends_with_question
    assert _output_ends_with_question("Should I proceed?") is True
    assert _output_ends_with_question("Done.") is False


# ---------- _maybe_propose_memory auto-apply on question ----------


def test_propose_diff_auto_applies_on_question(janus_home, monkeypatch, capsys):
    """When output ends with a question, the y/N prompt is SKIPPED
    and memory is auto-applied silently. No input() call."""
    pytest.importorskip("rich")
    from janus import cli_rich, memory, config

    # Stub propose_diff to return one op + one card.
    fake_ops = [{"category": "project", "section": "## Test",
                 "op": "append", "lines": ["test note"]}]
    monkeypatch.setattr(
        memory, "propose_diff",
        lambda req, out: {"ops": fake_ops, "cards": []},
    )
    applied = []
    monkeypatch.setattr(memory, "apply", lambda ops: applied.append(list(ops)))
    monkeypatch.setattr(memory, "apply_cards", lambda cards, gateway="": [])

    monkeypatch.setattr(config, "MEMORY_PROPOSE_ENABLED", True)
    monkeypatch.delenv("JANUS_MEMORY_PROMPT_ALWAYS", raising=False)

    # Stub input to fail loudly if anyone calls it during the
    # question-ended path.
    def _no_input(*a, **kw):
        raise AssertionError(
            "v1.24.5: should not prompt y/N when assistant ended "
            "with a question"
        )
    monkeypatch.setattr("builtins.input", _no_input)

    class _NullConsole:
        def print(self, *a, **kw): pass

    cli_rich._maybe_propose_memory(
        _NullConsole(),
        req="run pytest",
        output="2267 passed. Want me to install pytest-asyncio?",
    )
    # Op was auto-applied without any prompt.
    assert applied == [fake_ops]


def test_propose_diff_still_prompts_on_non_question(
    janus_home, monkeypatch,
):
    """Statements / non-questions still go through the y/N prompt
    (preserves the prior behavior for normal turns)."""
    pytest.importorskip("rich")
    from janus import cli_rich, memory, config
    fake_ops = [{"category": "project", "section": "## Test",
                 "op": "append", "lines": ["x"]}]
    monkeypatch.setattr(
        memory, "propose_diff",
        lambda req, out: {"ops": fake_ops, "cards": []},
    )
    monkeypatch.setattr(memory, "apply", lambda ops: None)
    monkeypatch.setattr(memory, "apply_cards", lambda cards, gateway="": [])
    monkeypatch.setattr(config, "MEMORY_PROPOSE_ENABLED", True)
    monkeypatch.delenv("JANUS_MEMORY_PROMPT_ALWAYS", raising=False)

    prompted = []
    monkeypatch.setattr(
        "builtins.input",
        lambda _msg="": prompted.append(_msg) or "n",
    )
    # Also patch prompt_toolkit if installed.
    try:
        import prompt_toolkit
        monkeypatch.setattr(
            prompt_toolkit, "prompt",
            lambda *a, **kw: prompted.append(a[0] if a else "") or "n",
        )
    except ImportError:
        pass

    class _NullConsole:
        def print(self, *a, **kw): pass

    cli_rich._maybe_propose_memory(
        _NullConsole(),
        req="run pytest",
        output="Done. All 2267 tests passed.",
    )
    # Should have prompted (statement, not question).
    assert prompted, "non-question replies still need y/N approval"


def test_propose_diff_force_prompt_via_env(janus_home, monkeypatch):
    """JANUS_MEMORY_PROMPT_ALWAYS=1 reverts to the old behavior even
    when the assistant ends with a question."""
    pytest.importorskip("rich")
    from janus import cli_rich, memory, config

    monkeypatch.setattr(
        memory, "propose_diff",
        lambda req, out: {"ops": [{"category": "user", "section": "##x",
                                    "op": "append", "lines": ["x"]}],
                          "cards": []},
    )
    monkeypatch.setattr(memory, "apply", lambda ops: None)
    monkeypatch.setattr(memory, "apply_cards", lambda cards, gateway="": [])
    monkeypatch.setattr(config, "MEMORY_PROPOSE_ENABLED", True)
    monkeypatch.setenv("JANUS_MEMORY_PROMPT_ALWAYS", "1")

    prompted = []
    monkeypatch.setattr(
        "builtins.input",
        lambda _msg="": prompted.append(_msg) or "n",
    )
    try:
        import prompt_toolkit
        monkeypatch.setattr(
            prompt_toolkit, "prompt",
            lambda *a, **kw: prompted.append(a[0] if a else "") or "n",
        )
    except ImportError:
        pass

    class _NullConsole:
        def print(self, *a, **kw): pass

    cli_rich._maybe_propose_memory(
        _NullConsole(),
        req="run pytest",
        output="Want me to install pytest-asyncio?",
    )
    assert prompted, (
        "JANUS_MEMORY_PROMPT_ALWAYS=1 should force the y/N prompt"
    )
