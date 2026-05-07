"""Tests for janus/at_mentions.py — `@path` file references in user
input (v1.25.1 Phase 1a).

The feature: user types ``what does @src/foo.py do?`` → the model
sees the file contents inlined inline. Conservative expansion:
- Only @-tokens at start-of-string or after whitespace expand
  (so ``user@domain.com`` doesn't false-match)
- Workspace-bounded via security.resolve_within
- Binary files refuse
- Big files truncate with a marker
- Failed expansions leave the @-token literal so the user sees
  their typo in the log
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------- Token regex ----------


def test_token_matches_at_start_of_string():
    from janus import at_mentions
    out, log = at_mentions.expand_at_mentions(
        "@pyproject.toml please", workspace="."
    )
    assert log[0]["path"] == "pyproject.toml"


def test_token_matches_after_whitespace():
    from janus import at_mentions
    out, log = at_mentions.expand_at_mentions(
        "look at @pyproject.toml ok", workspace="."
    )
    assert log[0]["path"] == "pyproject.toml"


def test_token_does_not_match_inside_email(tmp_path):
    from janus import at_mentions
    text = "ping user@domain.com about it"
    out, log = at_mentions.expand_at_mentions(text, workspace=str(tmp_path))
    # No @-mention should fire — domain.com isn't a workspace path,
    # AND the @ isn't preceded by whitespace.
    assert log == []
    assert out == text


def test_token_strips_trailing_punctuation():
    """Sentence-final punctuation shouldn't break the path match.
    `look at @foo.py.` → resolves to `foo.py`, period stays in text."""
    from janus import at_mentions
    out, log = at_mentions.expand_at_mentions(
        "see @pyproject.toml.", workspace="."
    )
    assert any(e["path"] == "pyproject.toml" for e in log)
    assert out.endswith(".")


# ---------- Expansion ----------


def test_expansion_inlines_file_contents(tmp_path):
    from janus import at_mentions
    f = tmp_path / "hello.txt"
    f.write_text("hello world\n")
    out, log = at_mentions.expand_at_mentions(
        "what does @hello.txt say?", workspace=str(tmp_path),
    )
    assert "[file: hello.txt]" in out
    assert "hello world" in out
    assert log[0]["status"] == "ok"


def test_expansion_uses_fenced_block(tmp_path):
    from janus import at_mentions
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    out, _ = at_mentions.expand_at_mentions("@x.py", workspace=str(tmp_path))
    assert "```" in out  # opens
    assert out.count("```") == 2  # opens + closes


def test_expansion_handles_multiple_mentions(tmp_path):
    from janus import at_mentions
    (tmp_path / "a.txt").write_text("AAA")
    (tmp_path / "b.txt").write_text("BBB")
    out, log = at_mentions.expand_at_mentions(
        "compare @a.txt and @b.txt", workspace=str(tmp_path),
    )
    assert "AAA" in out and "BBB" in out
    assert {e["path"] for e in log} == {"a.txt", "b.txt"}


def test_expansion_preserves_surrounding_text(tmp_path):
    from janus import at_mentions
    (tmp_path / "f.txt").write_text("contents")
    out, _ = at_mentions.expand_at_mentions(
        "before @f.txt after", workspace=str(tmp_path),
    )
    assert out.startswith("before ")
    assert out.endswith("after")


# ---------- Failure modes leave token literal ----------


def test_missing_file_leaves_token_literal(tmp_path):
    from janus import at_mentions
    out, log = at_mentions.expand_at_mentions(
        "look at @nope.txt", workspace=str(tmp_path),
    )
    assert "@nope.txt" in out
    assert log[0]["status"] == "missing"


def test_binary_file_leaves_token_literal(tmp_path):
    from janus import at_mentions
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02\x03\xff" * 200)
    out, log = at_mentions.expand_at_mentions(
        "@blob.bin", workspace=str(tmp_path),
    )
    assert "@blob.bin" in out
    assert log[0]["status"] == "binary"


def test_huge_file_skipped(tmp_path):
    from janus import at_mentions
    f = tmp_path / "huge.log"
    f.write_text("line\n" * 200_000)  # ~1MB, way beyond 5x cap
    out, log = at_mentions.expand_at_mentions(
        "@huge.log", workspace=str(tmp_path), max_bytes=10_000,
    )
    assert "@huge.log" in out
    assert log[0]["status"] == "too_big_skipped"


def test_truncation_marker_present_for_big_files(tmp_path):
    """File larger than max_bytes but ≤ 5x max_bytes truncates with a
    visible marker. Larger than 5x = refused as too_big_skipped."""
    from janus import at_mentions
    f = tmp_path / "biggish.log"
    f.write_text("x" * 30_000)
    out, log = at_mentions.expand_at_mentions(
        "@biggish.log", workspace=str(tmp_path), max_bytes=10_000,
    )
    assert "[... truncated:" in out
    assert log[0]["status"] == "truncated"


def test_directory_target_leaves_token_literal(tmp_path):
    from janus import at_mentions
    (tmp_path / "subdir").mkdir()
    out, log = at_mentions.expand_at_mentions(
        "@subdir", workspace=str(tmp_path),
    )
    assert "@subdir" in out
    assert log[0]["status"] == "missing"


# ---------- Workspace boundary ----------


def test_path_traversal_refused(tmp_path):
    """`@../etc/passwd` must not escape the workspace."""
    from janus import at_mentions
    (tmp_path / "ws").mkdir()
    out, log = at_mentions.expand_at_mentions(
        "@../etc/passwd", workspace=str(tmp_path / "ws"),
    )
    assert "@../etc/passwd" in out
    # status is "missing" because resolve_within returned None
    assert log[0]["status"] == "missing"


def test_no_at_token_returns_input_unchanged(tmp_path):
    from janus import at_mentions
    text = "no mentions here, just words"
    out, log = at_mentions.expand_at_mentions(text, workspace=str(tmp_path))
    assert out == text
    assert log == []


def test_empty_input_safe():
    from janus import at_mentions
    out, log = at_mentions.expand_at_mentions("", workspace=".")
    assert out == ""
    assert log == []


# ---------- Workspace file listing for completer ----------


def test_list_workspace_files_returns_immediate_entries(tmp_path):
    from janus import at_mentions
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "sub").mkdir()
    files = at_mentions.list_workspace_files(workspace=str(tmp_path))
    assert "a.py" in files
    assert "b.py" in files
    assert "sub/" in files


def test_list_workspace_files_directories_first(tmp_path):
    from janus import at_mentions
    (tmp_path / "z.py").write_text("")
    (tmp_path / "alpha").mkdir()
    files = at_mentions.list_workspace_files(workspace=str(tmp_path))
    # 'alpha/' (sort_key 0) before 'z.py' (sort_key 1)
    assert files.index("alpha/") < files.index("z.py")


def test_list_workspace_files_filters_by_prefix(tmp_path):
    from janus import at_mentions
    (tmp_path / "match_one.py").write_text("")
    (tmp_path / "match_two.py").write_text("")
    (tmp_path / "other.py").write_text("")
    files = at_mentions.list_workspace_files(
        workspace=str(tmp_path), prefix="match",
    )
    assert "match_one.py" in files
    assert "match_two.py" in files
    assert "other.py" not in files


def test_list_workspace_files_descends_subdirectory(tmp_path):
    from janus import at_mentions
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "foo.py").write_text("")
    (sub / "bar.py").write_text("")
    files = at_mentions.list_workspace_files(
        workspace=str(tmp_path), prefix="src/",
    )
    assert "src/foo.py" in files
    assert "src/bar.py" in files


def test_list_workspace_files_skips_dot_dirs(tmp_path):
    from janus import at_mentions
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "src").mkdir()
    files = at_mentions.list_workspace_files(workspace=str(tmp_path))
    assert ".git/" not in files
    assert "node_modules/" not in files
    assert "__pycache__/" not in files
    assert "src/" in files


def test_list_workspace_files_caps_results(tmp_path):
    from janus import at_mentions
    for i in range(60):
        (tmp_path / f"f{i:02d}.txt").write_text("")
    files = at_mentions.list_workspace_files(
        workspace=str(tmp_path), max_results=10,
    )
    assert len(files) == 10


# ---------- cli_rich wire-up ----------


def test_cli_rich_calls_expand_at_mentions():
    """Source-level pin: cli_rich's chat loop calls expand_at_mentions
    on user input before passing it to app.run_turn."""
    pytest.importorskip("rich")
    pytest.importorskip("prompt_toolkit")
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "expand_at_mentions" in src, (
        "cli_rich should call expand_at_mentions to inline @path "
        "references before the model sees the message"
    )
    assert "at_mentions" in src


def test_cli_rich_completer_handles_at_path():
    """Source-level pin: SlashCompleter handles @path completion in
    addition to /slash commands."""
    pytest.importorskip("rich")
    pytest.importorskip("prompt_toolkit")
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "_at_completions" in src, (
        "cli_rich should have an @path completer alongside the slash "
        "completer"
    )
