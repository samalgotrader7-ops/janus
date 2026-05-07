"""Tests for v1.27.1 — verification by default (Phase 3 #3).

After a code edit (fs_write/fs_edit/fs_multi_edit on a .py file),
executor.chat runs the targeted pytest file for the edited source
and appends the pass/fail block to the tool result the model sees.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from janus import verification


# ============================================================
# is_code_edit
# ============================================================


def test_is_code_edit_fs_write_py():
    assert verification.is_code_edit("fs_write", {"path": "foo.py"}) is True


def test_is_code_edit_fs_edit_py():
    assert verification.is_code_edit("fs_edit", {"path": "src/foo.py"}) is True


def test_is_code_edit_fs_multi_edit_py():
    assert verification.is_code_edit("fs_multi_edit", {"path": "x.py"}) is True


def test_is_code_edit_non_py_skipped():
    """Markdown / JSON / YAML edits are not verified (v1.27.1 = Python only)."""
    assert verification.is_code_edit("fs_write", {"path": "README.md"}) is False
    assert verification.is_code_edit("fs_write", {"path": "package.json"}) is False
    assert verification.is_code_edit("fs_write", {"path": "config.yaml"}) is False


def test_is_code_edit_non_editing_tool_skipped():
    """fs_read / shell / etc. don't trigger verification."""
    assert verification.is_code_edit("fs_read", {"path": "foo.py"}) is False
    assert verification.is_code_edit("shell", {"command": "echo hi"}) is False


def test_is_code_edit_empty_path_skipped():
    assert verification.is_code_edit("fs_write", {"path": ""}) is False
    assert verification.is_code_edit("fs_write", {}) is False
    assert verification.is_code_edit("fs_write", None) is False


# ============================================================
# is_python_project
# ============================================================


def test_is_python_project_pyproject_toml(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert verification.is_python_project(tmp_path) is True


def test_is_python_project_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    assert verification.is_python_project(tmp_path) is True


def test_is_python_project_setup_cfg(tmp_path):
    (tmp_path / "setup.cfg").write_text("[metadata]\nname=x\n")
    assert verification.is_python_project(tmp_path) is True


def test_is_python_project_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    assert verification.is_python_project(tmp_path) is True


def test_is_python_project_test_dir_singular(tmp_path):
    (tmp_path / "test").mkdir()
    assert verification.is_python_project(tmp_path) is True


def test_is_python_project_empty_dir_no(tmp_path):
    assert verification.is_python_project(tmp_path) is False


def test_is_python_project_only_js_files_no(tmp_path):
    """Random JS project should not be flagged as Python."""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "index.js").write_text("// js\n")
    assert verification.is_python_project(tmp_path) is False


def test_is_python_project_missing_workspace_no(tmp_path):
    """Defensive: workspace path that doesn't exist returns False."""
    fake = tmp_path / "nope"
    assert verification.is_python_project(fake) is False


# ============================================================
# find_test_targets
# ============================================================


def test_find_targets_tests_test_stem_py(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def x(): pass")
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "test_foo.py"
    target.write_text("def test_x(): pass")
    found = verification.find_test_targets(tmp_path, "src/foo.py")
    assert len(found) == 1
    assert found[0].name == "test_foo.py"


def test_find_targets_recursive_under_tests(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def x(): pass")
    (tmp_path / "tests" / "subdir").mkdir(parents=True)
    target = tmp_path / "tests" / "subdir" / "test_foo.py"
    target.write_text("def test_x(): pass")
    found = verification.find_test_targets(tmp_path, "src/foo.py")
    assert any(t.name == "test_foo.py" for t in found)


def test_find_targets_sibling_test_file(tmp_path):
    """test_foo.py next to foo.py — supported convention."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def x(): pass")
    (src / "test_foo.py").write_text("def test_x(): pass")
    found = verification.find_test_targets(tmp_path, "src/foo.py")
    assert any(t.parent == src for t in found)


def test_find_targets_no_match_returns_empty(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x")
    (tmp_path / "tests").mkdir()
    # No matching test file.
    found = verification.find_test_targets(tmp_path, "src/foo.py")
    assert found == []


def test_find_targets_test_file_itself(tmp_path):
    """Editing tests/test_foo.py → run that file directly."""
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "test_foo.py"
    target.write_text("def test_x(): pass")
    found = verification.find_test_targets(tmp_path, "tests/test_foo.py")
    assert any(t == target.resolve() for t in found)


def test_find_targets_no_dupes(tmp_path):
    """If both tests/test_foo.py and tests/sub/test_foo.py exist, both
    listed but no duplicates."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x")
    (tmp_path / "tests").mkdir()
    a = tmp_path / "tests" / "test_foo.py"
    a.write_text("def test_a(): pass")
    found = verification.find_test_targets(tmp_path, "src/foo.py")
    # Each path appears at most once
    seen = [str(t) for t in found]
    assert len(seen) == len(set(seen))


def test_find_targets_absolute_edited_path(tmp_path):
    """Absolute path passed in should still resolve correctly."""
    (tmp_path / "src").mkdir()
    src_file = tmp_path / "src" / "foo.py"
    src_file.write_text("x")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_x(): pass")
    found = verification.find_test_targets(tmp_path, str(src_file))
    assert len(found) >= 1


# ============================================================
# verify_python — actual subprocess invocation
# ============================================================


def _make_python_project(tmp_path: Path, src_body: str, test_body: str) -> Path:
    """Set up a minimal Python project that pytest can run."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='vproj'\nversion='0.0.1'\n",
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text(src_body, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_foo.py").write_text(test_body, encoding="utf-8")
    return tmp_path


def test_verify_python_passing(tmp_path):
    _make_python_project(
        tmp_path,
        src_body="def add(a, b): return a + b\n",
        test_body=(
            "import sys, os\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            "from foo import add\n"
            "def test_add(): assert add(2, 3) == 5\n"
        ),
    )
    result = verification.verify_python(tmp_path, "src/foo.py", timeout=30)
    assert result is not None
    assert result["passed"] is True
    assert result["runner"] == "pytest"
    assert result["timed_out"] is False
    assert any("test_foo.py" in t for t in result["targets"])


def test_verify_python_failing(tmp_path):
    _make_python_project(
        tmp_path,
        src_body="def add(a, b): return a + b\n",
        test_body=(
            "import sys, os\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            "from foo import add\n"
            "def test_add(): assert add(2, 3) == 99  # wrong\n"
        ),
    )
    result = verification.verify_python(tmp_path, "src/foo.py", timeout=30)
    assert result is not None
    assert result["passed"] is False
    assert result["exit_code"] != 0
    assert result["timed_out"] is False
    # Failure output should be captured
    assert "AssertionError" in result["output_preview"] or "assert" in result["output_preview"].lower()


def test_verify_python_no_test_file_returns_none(tmp_path):
    """No tests/test_foo.py → verify returns None (skip)."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def x(): pass")
    result = verification.verify_python(tmp_path, "src/foo.py", timeout=10)
    assert result is None


def test_verify_python_pytest_missing_returns_none(tmp_path, monkeypatch):
    """If pytest isn't on PATH, return None (don't crash)."""
    _make_python_project(
        tmp_path, src_body="x=1\n", test_body="def test_x(): pass\n",
    )
    monkeypatch.setattr(verification.shutil, "which", lambda cmd: None)
    result = verification.verify_python(tmp_path, "src/foo.py", timeout=10)
    assert result is None


def test_verify_python_handles_timeout(tmp_path, monkeypatch):
    """If pytest times out, return a TIMED OUT result, don't crash."""
    _make_python_project(
        tmp_path, src_body="x=1\n", test_body="def test_x(): pass\n",
    )

    def _fake_run(*a, **kw):
        raise __import__("subprocess").TimeoutExpired(
            cmd="pytest", timeout=kw.get("timeout", 1),
            output=b"some output\n",
            stderr=b"",
        )

    monkeypatch.setattr(verification.subprocess, "run", _fake_run)
    result = verification.verify_python(tmp_path, "src/foo.py", timeout=1)
    assert result is not None
    assert result["timed_out"] is True
    assert result["passed"] is False


def test_verify_python_handles_run_exception(tmp_path, monkeypatch):
    """Random subprocess exception is contained — returns a result
    rather than propagating."""
    _make_python_project(
        tmp_path, src_body="x=1\n", test_body="def test_x(): pass\n",
    )

    def _explode(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(verification.subprocess, "run", _explode)
    result = verification.verify_python(tmp_path, "src/foo.py", timeout=10)
    assert result is not None
    assert result["passed"] is False
    assert "OSError" in result["output_preview"] or "disk full" in result["output_preview"]


# ============================================================
# format_result
# ============================================================


def test_format_result_passing():
    result = {
        "passed": True, "exit_code": 0, "runner": "pytest",
        "targets": ["tests/test_foo.py"],
        "output_preview": "1 passed in 0.05s",
        "timed_out": False,
    }
    out = verification.format_result(result)
    assert "PASSED" in out
    assert "1 passed" in out
    assert "test_foo.py" in out


def test_format_result_failing():
    result = {
        "passed": False, "exit_code": 1, "runner": "pytest",
        "targets": ["tests/test_foo.py"],
        "output_preview": (
            "tests/test_foo.py::test_add: AssertionError: 99 != 5\n"
            "1 failed in 0.06s"
        ),
        "timed_out": False,
    }
    out = verification.format_result(result)
    assert "FAILED" in out
    assert "AssertionError" in out


def test_format_result_timed_out():
    result = {
        "passed": False, "exit_code": -1, "runner": "pytest",
        "targets": ["tests/test_x.py"],
        "output_preview": "partial output...",
        "timed_out": True,
    }
    out = verification.format_result(result)
    assert "TIMED OUT" in out


# ============================================================
# maybe_verify — the public hook
# ============================================================


def test_maybe_verify_off_when_env_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_AUTO_VERIFY", "0")
    _make_python_project(
        tmp_path,
        src_body="x=1\n",
        test_body="def test_x(): assert True\n",
    )
    result = verification.maybe_verify(
        "fs_write", {"path": "src/foo.py"}, "wrote it", workspace=tmp_path,
    )
    assert result is None


def test_maybe_verify_skips_non_code_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_AUTO_VERIFY", "1")
    _make_python_project(
        tmp_path, src_body="x=1\n", test_body="def test_x(): pass\n",
    )
    # Markdown edit — skipped
    result = verification.maybe_verify(
        "fs_write", {"path": "README.md"}, "wrote", workspace=tmp_path,
    )
    assert result is None


def test_maybe_verify_skips_error_results(tmp_path, monkeypatch):
    """Don't waste a subprocess if the edit didn't even succeed."""
    monkeypatch.setenv("JANUS_AUTO_VERIFY", "1")
    _make_python_project(
        tmp_path, src_body="x=1\n", test_body="def test_x(): pass\n",
    )
    result = verification.maybe_verify(
        "fs_write", {"path": "src/foo.py"},
        "error: file refused", workspace=tmp_path,
    )
    assert result is None


def test_maybe_verify_skips_refused_results(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_AUTO_VERIFY", "1")
    _make_python_project(
        tmp_path, src_body="x=1\n", test_body="def test_x(): pass\n",
    )
    result = verification.maybe_verify(
        "fs_write", {"path": "src/foo.py"},
        "refused: not in workspace", workspace=tmp_path,
    )
    assert result is None


def test_maybe_verify_skips_non_python_project(tmp_path, monkeypatch):
    """Empty workspace + .py edit → skipped (not a Python project)."""
    monkeypatch.setenv("JANUS_AUTO_VERIFY", "1")
    (tmp_path / "src").mkdir()
    result = verification.maybe_verify(
        "fs_write", {"path": "src/foo.py"}, "wrote",
        workspace=tmp_path,
    )
    assert result is None


def test_maybe_verify_runs_when_all_conditions_met(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_AUTO_VERIFY", "1")
    monkeypatch.setenv("JANUS_VERIFY_TIMEOUT", "10")
    _make_python_project(
        tmp_path,
        src_body="def x(): return 5\n",
        test_body=(
            "import sys, os\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
            "from foo import x\n"
            "def test_x(): assert x() == 5\n"
        ),
    )
    result = verification.maybe_verify(
        "fs_write", {"path": "src/foo.py"}, "wrote 12 bytes",
        workspace=tmp_path,
    )
    assert result is not None
    assert result["passed"] is True


def test_maybe_verify_uses_env_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_AUTO_VERIFY", "1")
    monkeypatch.setenv("JANUS_VERIFY_TIMEOUT", "7")
    _make_python_project(
        tmp_path, src_body="x=1\n",
        test_body="def test_x(): assert True\n",
    )
    captured = {}

    real_verify_python = verification.verify_python

    def _spy(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return real_verify_python(*args, **kwargs)

    monkeypatch.setattr(verification, "verify_python", _spy)
    verification.maybe_verify(
        "fs_write", {"path": "src/foo.py"}, "ok",
        workspace=tmp_path,
    )
    assert captured["timeout"] == 7


def test_maybe_verify_invalid_timeout_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_AUTO_VERIFY", "1")
    monkeypatch.setenv("JANUS_VERIFY_TIMEOUT", "not-a-number")
    _make_python_project(
        tmp_path, src_body="x=1\n",
        test_body="def test_x(): pass\n",
    )
    captured = {}

    real_verify_python = verification.verify_python

    def _spy(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return real_verify_python(*args, **kwargs)

    monkeypatch.setattr(verification, "verify_python", _spy)
    verification.maybe_verify(
        "fs_write", {"path": "src/foo.py"}, "ok",
        workspace=tmp_path,
    )
    assert captured["timeout"] == verification.DEFAULT_TIMEOUT


# ============================================================
# Integration: hook in executor.chat
# ============================================================


def test_executor_chat_appends_verification_block_to_tool_result():
    """Source-level pin: chat() calls verification.maybe_verify after
    a tool result and appends the formatted block to content_for_model
    BEFORE messages.append. This is the contract the v1.27.1 release
    delivers."""
    import inspect
    from janus import executor
    src = inspect.getsource(executor.chat)
    # Hook point present
    assert "verification.maybe_verify" in src or "_verify.maybe_verify" in src
    assert "format_result" in src
    # Result is added BEFORE the tool message append
    verify_idx = src.find("maybe_verify")
    append_idx = src.rfind('"role": "tool"')
    assert verify_idx > -1 and append_idx > verify_idx, (
        "verification must run before the tool message is appended"
    )


def test_executor_chat_emits_verification_result_event():
    """When maybe_verify returns a result, chat() emits a
    `verification_result` on_step event so renderers can show
    pass/fail UI without re-parsing the tool result text."""
    import inspect
    from janus import executor
    src = inspect.getsource(executor.chat)
    assert '"type": "verification_result"' in src


def test_executor_chat_verification_failure_doesnt_break_loop():
    """Defensive: a verification subprocess crash must NOT break the
    chat loop. Pinned via `try: ... except Exception: pass` around
    the verification block."""
    import inspect
    from janus import executor
    src = inspect.getsource(executor.chat)
    # Find the try block enclosing maybe_verify
    block_start = src.find("try:")
    while block_start != -1:
        block_end = src.find("except Exception:", block_start)
        if block_end == -1:
            break
        block_body = src[block_start:block_end]
        if "maybe_verify" in block_body:
            return  # OK — verification is wrapped in try/except Exception
        block_start = src.find("try:", block_end)
    pytest.fail("verification call site is not wrapped in try/except Exception")


# ============================================================
# EVENT_TYPES vocabulary pin
# ============================================================


def test_verification_result_in_event_types():
    from janus.app import EVENT_TYPES
    assert "verification_result" in EVENT_TYPES
