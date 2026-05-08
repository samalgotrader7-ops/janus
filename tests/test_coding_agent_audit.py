"""tests/test_coding_agent_audit.py — v1.15.0 (Claude Code parity)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from janus import config, project_context, read_tracker
from janus.tools import default_registry
from janus.tools.fs import FsRead
from janus.tools.edit import FsEdit
from janus.tools.plan_mode import ExitPlanMode, PLAN_APPROVED, PLAN_REFUSED
from janus.tools.shell_bg import (
    ShellRunBg, ShellOutput, ShellKill, ShellList,
)


def _approve(*a, **kw):
    return True


def _deny(*a, **kw):
    return False


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(config, "TRIGGERS_DIR", home / "triggers")
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "USER_MODEL_FILE", home / "user.md")
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(config, "DAEMON_STATE", home / "daemon.state.json")
    monkeypatch.setattr(config, "EVALS_DIR", home / "evals")
    monkeypatch.setattr(config, "MCP_DIR", home / "mcp")
    monkeypatch.setattr(config, "CONVERSATIONS_DIR", home / "conversations")
    monkeypatch.setattr(config, "COMMANDS_DIR", home / "commands")
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", home / "swarms" / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", home / "swarms" / "runs")
    config.ensure_home()
    read_tracker.reset()


# ============================================================
# Read tracker (Edit conflict detection)
# ============================================================


def test_read_tracker_marks_after_fs_read(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    target = tmp_path / "x.txt"
    target.write_text("hello", encoding="utf-8")
    FsRead().run({"path": "x.txt"}, _approve)
    assert read_tracker.was_read_recently(target)


def test_fs_edit_refuses_when_unread(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    target = tmp_path / "x.txt"
    target.write_text("hello world", encoding="utf-8")
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "hello", "new_string": "hi"},
        _approve,
    )
    assert out.startswith("error: must fs_read")


def test_fs_edit_succeeds_after_read(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    target = tmp_path / "x.txt"
    target.write_text("hello world", encoding="utf-8")
    FsRead().run({"path": "x.txt"}, _approve)
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "hello", "new_string": "hi"},
        _approve,
    )
    assert "edited x.txt" in out
    assert target.read_text(encoding="utf-8") == "hi world"


def test_fs_edit_refuses_when_modified_externally(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    target = tmp_path / "x.txt"
    target.write_text("hello world", encoding="utf-8")
    FsRead().run({"path": "x.txt"}, _approve)
    # Wait + modify externally so mtime changes
    time.sleep(0.01)
    target.write_text("totally different content here", encoding="utf-8")
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "totally", "new_string": "X"},
        _approve,
    )
    assert "modified since" in out


def test_fs_edit_envvar_disables_check(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_FS_EDIT_REQUIRE_READ", "0")
    target = tmp_path / "x.txt"
    target.write_text("hello", encoding="utf-8")
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "hello", "new_string": "hi"},
        _approve,
    )
    assert "edited x.txt" in out


def test_fs_edit_then_edit_again_works(tmp_path, monkeypatch):
    """fs_edit re-marks the file as read so consecutive edits in the
    same turn don't spuriously fail the modification check."""
    _isolate_home(tmp_path, monkeypatch)
    target = tmp_path / "x.txt"
    target.write_text("AAA\nBBB\nCCC", encoding="utf-8")
    FsRead().run({"path": "x.txt"}, _approve)
    FsEdit().run({"path": "x.txt", "old_string": "AAA", "new_string": "XXX"}, _approve)
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "BBB", "new_string": "YYY"},
        _approve,
    )
    assert "edited x.txt" in out


# ============================================================
# ExitPlanMode
# ============================================================


def test_exit_plan_mode_approved():
    out = ExitPlanMode().run({"plan": "1. Read main.py\n2. Refactor"}, _approve)
    # v1.31.13: tool returns enriched guidance message with the
    # PLAN_APPROVED sentinel as a literal substring (preserved for
    # cli_rich's post-turn mode-switch detector).
    assert PLAN_APPROVED in out


def test_exit_plan_mode_refused():
    out = ExitPlanMode().run({"plan": "1. delete everything"}, _deny)
    # v1.31.13: tool returns enriched guidance message with the
    # PLAN_REFUSED sentinel as a literal substring.
    assert PLAN_REFUSED in out


def test_exit_plan_mode_rejects_empty_plan():
    out = ExitPlanMode().run({"plan": ""}, _approve)
    assert out.startswith("error:")


def test_exit_plan_mode_in_default_registry():
    reg = default_registry()
    assert "exit_plan_mode" in reg.names()


def test_exit_plan_mode_is_read_class():
    """Read-class so it's not denied in mode=plan (which denies write/exec)."""
    assert ExitPlanMode.risk == "read"


# ============================================================
# Project context (CLAUDE.md / JANUS.md / AGENTS.md auto-load)
# ============================================================


def test_find_instruction_files_finds_janus_md(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    (tmp_path / "JANUS.md").write_text("# Project rules\n", encoding="utf-8")
    files = project_context.find_instruction_files()
    assert any(f.name == "JANUS.md" for f in files)


def test_find_instruction_files_finds_claude_md(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    (tmp_path / "CLAUDE.md").write_text("# claude rules\n", encoding="utf-8")
    files = project_context.find_instruction_files()
    assert any(f.name == "CLAUDE.md" for f in files)


def test_find_instruction_files_priority_within_dir(tmp_path, monkeypatch):
    """Per directory, only the FIRST matching filename wins —
    JANUS.md beats CLAUDE.md in the same dir."""
    _isolate_home(tmp_path, monkeypatch)
    (tmp_path / "JANUS.md").write_text("# janus", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# claude", encoding="utf-8")
    files = project_context.find_instruction_files()
    names = [f.name for f in files]
    # JANUS.md is in the priority list before CLAUDE.md
    assert "JANUS.md" in names
    assert "CLAUDE.md" not in names  # second file in same dir is skipped


def test_find_instruction_files_walks_up(tmp_path, monkeypatch):
    """Walk-up finds CLAUDE.md in repo root even when CWD is deep inside."""
    _isolate_home(tmp_path, monkeypatch)
    repo_root = tmp_path / "repo"
    nested = repo_root / "src" / "deep"
    nested.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    (repo_root / "CLAUDE.md").write_text("# root rules", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE", nested)
    files = project_context.find_instruction_files()
    assert any(f.name == "CLAUDE.md" and f.parent == repo_root for f in files)


def test_find_instruction_files_stops_at_git_boundary(tmp_path, monkeypatch):
    """Don't escape the repo even if a parent dir has CLAUDE.md."""
    _isolate_home(tmp_path, monkeypatch)
    parent = tmp_path / "outside"
    parent.mkdir()
    (parent / "CLAUDE.md").write_text("# outside the repo", encoding="utf-8")
    repo = parent / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "JANUS.md").write_text("# inside", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE", repo)
    files = project_context.find_instruction_files()
    # repo/JANUS.md should be found
    assert any(f.name == "JANUS.md" for f in files)
    # parent/CLAUDE.md is OUTSIDE the .git boundary — must NOT load
    # (check by the CONTENT, not path string — tmp dir name varies)
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "outside the repo" not in text


def test_load_block_includes_instructions(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    (tmp_path / "JANUS.md").write_text(
        "# Project\n\nRule: use pnpm not npm.", encoding="utf-8",
    )
    block = project_context.load_block()
    assert "Project instructions" in block
    assert "use pnpm not npm" in block


def test_load_block_empty_with_no_files(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    assert project_context.load_block() == ""


def test_load_block_respects_env_disable(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_PROJECT_INSTRUCTIONS", "0")
    (tmp_path / "JANUS.md").write_text("# rules", encoding="utf-8")
    assert project_context.load_block() == ""


def test_load_block_truncates_huge_file(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    (tmp_path / "JANUS.md").write_text("X" * 50_000, encoding="utf-8")
    block = project_context.load_block()
    assert "[truncated for prompt]" in block


def test_memory_prepend_includes_project_block(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    (tmp_path / "JANUS.md").write_text("# rules", encoding="utf-8")
    from janus import memory
    out = memory.prepend_for_prompt()
    assert "Project instructions" in out


# ============================================================
# Background shell
# ============================================================


def test_shell_run_bg_rejects_empty_command():
    out = ShellRunBg().run({"command": ""}, _approve)
    assert out.startswith("error:")


def test_shell_run_bg_refusal():
    out = ShellRunBg().run({"command": "echo hi"}, _deny)
    assert out.startswith("refused:")


def test_shell_run_bg_launches_and_records_state(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = ShellRunBg().run(
        {"command": "echo hello"}, _approve,
    )
    assert "shell_id:" in out
    assert "pid:" in out
    assert "status: running" in out

    # Extract the shell_id from output
    import re
    m = re.search(r"shell_id:\s*(\S+)", out)
    assert m
    shell_id = m.group(1)

    # State directory + files exist
    d = config.HOME / "shells" / shell_id
    assert d.is_dir()
    assert (d / "pid").is_file()
    assert (d / "cmd").is_file()
    assert (d / "started").is_file()


def test_shell_output_returns_eventually(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = ShellRunBg().run({"command": "echo hello-world"}, _approve)
    import re
    shell_id = re.search(r"shell_id:\s*(\S+)", out).group(1)

    # Wait for the echo to complete (it's fast).
    # Loop because Windows + bash startup can take a moment.
    deadline = time.time() + 5
    while time.time() < deadline:
        result = ShellOutput().run({"shell_id": shell_id}, _approve)
        if "hello-world" in result:
            return
        time.sleep(0.2)
    pytest.fail(f"Never saw 'hello-world' in output. Last: {result!r}")


def test_shell_output_unknown_id():
    out = ShellOutput().run({"shell_id": "ghost"}, _approve)
    assert out.startswith("error:")


def test_shell_kill_unknown_id():
    out = ShellKill().run({"shell_id": "ghost"}, _approve)
    assert out.startswith("error:")


def test_shell_list_empty(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = ShellList().run({}, _approve)
    assert "no shells" in out


def test_shell_list_after_launch(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ShellRunBg().run({"command": "echo x", "label": "mybuild"}, _approve)
    out = ShellList().run({}, _approve)
    assert "mybuild" in out


def test_shell_run_bg_max_concurrent(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    # Pretend we already have N running by writing fake state.
    from janus.tools.shell_bg import (
        SHELL_BG_MAX_RUNNING, _shell_dir, _write_status,
    )
    for i in range(SHELL_BG_MAX_RUNNING):
        d = _shell_dir(f"fake-{i}")
        d.mkdir(parents=True, exist_ok=True)
        # Write THIS process's pid so _is_running returns True
        import os as _os
        (d / "pid").write_text(str(_os.getpid()), encoding="utf-8")
        (d / "started").write_text("2030-01-01T00:00:00+00:00", encoding="utf-8")
        _write_status(f"fake-{i}", "running")
    out = ShellRunBg().run({"command": "echo hi"}, _approve)
    assert "max" in out and "running" in out


def test_shell_tools_in_default_registry():
    reg = default_registry()
    for name in ("shell_run_bg", "shell_output", "shell_kill", "shell_list"):
        assert name in reg.names()


# ============================================================
# System prompt has coding conventions
# ============================================================


def test_system_prompt_mentions_file_line_convention():
    from janus.executor import JANUS_CHAT_SYSTEM
    assert "file:line" in JANUS_CHAT_SYSTEM.lower() or "file_path:line_number" in JANUS_CHAT_SYSTEM


def test_system_prompt_mentions_dedicated_tools_over_shell():
    from janus.executor import JANUS_CHAT_SYSTEM
    assert "fs_glob" in JANUS_CHAT_SYSTEM
    assert "fs_grep" in JANUS_CHAT_SYSTEM


def test_system_prompt_mentions_read_before_edit():
    from janus.executor import JANUS_CHAT_SYSTEM
    # Some hint about reading before editing
    s = JANUS_CHAT_SYSTEM.lower()
    assert "fs_read" in s
    assert "fs_edit" in s


def test_system_prompt_mentions_background_shell():
    from janus.executor import JANUS_CHAT_SYSTEM
    assert "shell_run_bg" in JANUS_CHAT_SYSTEM


def test_system_prompt_mentions_exit_plan_mode():
    from janus.executor import JANUS_CHAT_SYSTEM
    assert "exit_plan_mode" in JANUS_CHAT_SYSTEM
