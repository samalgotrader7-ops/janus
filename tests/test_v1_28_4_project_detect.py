"""Tests for v1.28.4 — project-context tool gating (Phase 4).

v1.28.4 adds project-type detection (python / node / rust / go /
mixed / unknown) and surfaces it as:
  * a one-line block in the system prompt (between read_tracker
    context and JANUS_CHAT_SYSTEM)
  * a new ``/project`` slash command
  * an env override JANUS_PROJECT_TYPE
"""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import config, project_detect
from janus.project_detect import (
    detect_project_type,
    render_prompt_block,
    render_summary,
    ProjectInfo,
    PYTHON_INDICATORS,
    NODE_INDICATORS,
    RUST_INDICATORS,
    GO_INDICATORS,
    TEST_COMMANDS,
)


def _isolate_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    # Ensure no env override leaks
    monkeypatch.delenv("JANUS_PROJECT_TYPE", raising=False)
    return tmp_path


# ============================================================
# detect_project_type — auto-detection
# ============================================================


def test_detect_python_via_pyproject(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    info = detect_project_type(ws)
    assert info.type == "python"
    assert "python:pyproject.toml" in info.indicators
    assert info.source == "auto"


def test_detect_python_via_setup_py(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "setup.py").write_text("from setuptools import setup\n")
    info = detect_project_type(ws)
    assert info.type == "python"


def test_detect_python_via_pipfile(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "Pipfile").write_text("[packages]\n")
    info = detect_project_type(ws)
    assert info.type == "python"


def test_detect_node_via_package_json(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "package.json").write_text('{"name": "x"}')
    info = detect_project_type(ws)
    assert info.type == "node"


def test_detect_rust_via_cargo_toml(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "Cargo.toml").write_text("[package]\nname='x'\n")
    info = detect_project_type(ws)
    assert info.type == "rust"


def test_detect_go_via_go_mod(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "go.mod").write_text("module x\n")
    info = detect_project_type(ws)
    assert info.type == "go"


def test_detect_mixed_when_multiple(tmp_path, monkeypatch):
    """Workspace with both Python and Node manifests = mixed."""
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    (ws / "package.json").write_text('{"name": "x"}')
    info = detect_project_type(ws)
    assert info.type == "mixed"
    # Both kinds of indicators surface
    assert any("python:" in i for i in info.indicators)
    assert any("node:" in i for i in info.indicators)


def test_detect_unknown_when_no_markers(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    info = detect_project_type(ws)
    assert info.type == "unknown"
    assert info.indicators == []


def test_detect_handles_missing_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("JANUS_PROJECT_TYPE", raising=False)
    info = detect_project_type(tmp_path / "nope")
    assert info.type == "unknown"


def test_detect_collects_all_python_indicators(tmp_path, monkeypatch):
    """Multiple Python files all get listed (without inflating to mixed)."""
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    (ws / "setup.py").write_text("\n")
    (ws / "requirements.txt").write_text("\n")
    info = detect_project_type(ws)
    assert info.type == "python"
    assert len(info.indicators) >= 3


# ============================================================
# Env override
# ============================================================


def test_env_override_short_circuits(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    # Filesystem says python, env says rust → env wins
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setenv("JANUS_PROJECT_TYPE", "rust")
    info = detect_project_type(ws)
    assert info.type == "rust"
    assert info.source == "env"
    # Env override skips indicator gathering
    assert info.indicators == []


def test_env_override_lowercases(tmp_path, monkeypatch):
    _isolate_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_PROJECT_TYPE", "PYTHON")
    info = detect_project_type(tmp_path)
    assert info.type == "python"


def test_env_override_strips_whitespace(tmp_path, monkeypatch):
    _isolate_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_PROJECT_TYPE", "  rust  ")
    info = detect_project_type(tmp_path)
    assert info.type == "rust"


# ============================================================
# Test commands
# ============================================================


def test_test_command_python_is_pytest():
    assert TEST_COMMANDS["python"] == "pytest"


def test_test_command_node_is_npm_test():
    assert TEST_COMMANDS["node"] == "npm test"


def test_test_command_rust_is_cargo_test():
    assert TEST_COMMANDS["rust"] == "cargo test"


def test_test_command_go_is_go_test():
    assert TEST_COMMANDS["go"] == "go test ./..."


def test_info_test_command_populated(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    info = detect_project_type(ws)
    assert info.test_command == "pytest"


# ============================================================
# render_prompt_block
# ============================================================


def test_render_prompt_block_includes_type(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    info = detect_project_type(ws)
    block = render_prompt_block(info)
    assert "python" in block.lower()
    assert "Project type" in block


def test_render_prompt_block_empty_for_unknown():
    info = ProjectInfo(type="unknown")
    assert render_prompt_block(info) == ""


def test_render_prompt_block_includes_test_command(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "go.mod").write_text("module x\n")
    info = detect_project_type(ws)
    block = render_prompt_block(info)
    assert "go test" in block


def test_render_prompt_block_caps_indicator_list(tmp_path, monkeypatch):
    """Long indicator lists get truncated with a '+N more' marker."""
    ws = _isolate_workspace(tmp_path, monkeypatch)
    for f in PYTHON_INDICATORS:
        (ws / f).write_text("\n")
    info = detect_project_type(ws)
    block = render_prompt_block(info)
    if len(info.indicators) > 4:
        assert "more" in block


# ============================================================
# render_summary (for /project slash command)
# ============================================================


def test_render_summary_shows_type_source_workspace(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    info = detect_project_type(ws)
    out = render_summary(info)
    assert "python" in out
    assert "auto" in out
    assert str(ws) in out


def test_render_summary_lists_all_indicators(tmp_path, monkeypatch):
    ws = _isolate_workspace(tmp_path, monkeypatch)
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    (ws / "setup.py").write_text("\n")
    info = detect_project_type(ws)
    out = render_summary(info)
    assert "pyproject.toml" in out
    assert "setup.py" in out


def test_render_summary_empty_indicators_message(tmp_path, monkeypatch):
    _isolate_workspace(tmp_path, monkeypatch)
    info = detect_project_type(tmp_path)
    out = render_summary(info)
    assert "none" in out.lower()


# ============================================================
# Executor integration
# ============================================================


def test_build_chat_system_includes_project_block_for_known_type(tmp_path, monkeypatch):
    monkeypatch.delenv("JANUS_PROJECT_TYPE", raising=False)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    from janus import executor
    out = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="default",
        memory_preamble="",
        skill_body="",
    )
    assert "Project type:" in out
    assert "python" in out.lower()


def test_build_chat_system_omits_project_block_when_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("JANUS_PROJECT_TYPE", raising=False)
    from janus import executor
    out = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="default",
        memory_preamble="",
        skill_body="",
    )
    # Empty workspace → unknown → no block
    assert "## Project type:" not in out


def test_build_chat_system_project_block_after_context_before_rules(tmp_path, monkeypatch):
    """Project block lands AFTER read_tracker context and BEFORE
    JANUS_CHAT_SYSTEM rules."""
    monkeypatch.delenv("JANUS_PROJECT_TYPE", raising=False)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    from janus import executor, read_tracker
    read_tracker.reset()
    f = tmp_path / "x.py"
    f.write_text("y\n")
    read_tracker.mark_read(f)
    out = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="default",
        memory_preamble="MEM",
        skill_body="",
    )
    mem_pos = out.find("MEM")
    ctx_pos = out.find("Files already in your session context")
    proj_pos = out.find("## Project type:")
    rule_pos = out.find("EXPLANATION QUESTIONS")
    assert -1 < mem_pos < ctx_pos < proj_pos < rule_pos


# ============================================================
# cli_rich /project handler
# ============================================================


def test_cli_rich_project_command_registered():
    """`/project` is in BUILTIN_COMMANDS so /help lists it and the
    completer suggests it."""
    from janus.slash_dispatch import BUILTIN_COMMANDS
    names = [c.name for c in BUILTIN_COMMANDS]
    assert "/project" in names


def test_cli_rich_project_handler_exists():
    """Source-pin: cli_rich._dispatch routes /project to project_detect."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert 'cmd == "/project"' in src
    assert "project_detect" in src
    assert "render_summary" in src


# ============================================================
# Public exports
# ============================================================


def test_constants_exported():
    """The indicator catalogues are exported so external tooling can
    reference them (skill frontmatter validators, etc.)."""
    assert "pyproject.toml" in PYTHON_INDICATORS
    assert "package.json" in NODE_INDICATORS
    assert "Cargo.toml" in RUST_INDICATORS
    assert "go.mod" in GO_INDICATORS


def test_project_info_is_known_property():
    assert ProjectInfo(type="python").is_known is True
    assert ProjectInfo(type="unknown").is_known is False
    assert ProjectInfo(type="").is_known is False
    assert ProjectInfo(type="mixed").is_known is True
