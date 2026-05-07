"""
verification.py — auto-run targeted tests after code edits (v1.27.1).

When the model edits a Python source file, this module finds the
matching pytest target (by stem-name convention) and runs ONLY that
file. The pass/fail result is appended to the tool result the model
sees, so the model can react in the same turn — fix breakage before
moving on.

DESIGN CHOICES:

  * Targeted tests, not full suite. Running the whole suite after
    every fs_edit would cost minutes per turn for a non-trivial
    project. We map ``src/foo.py`` → ``tests/test_foo.py`` and run
    just that.

  * Python/pytest only in v1.27.1. Other ecosystems (Node / Rust /
    Go / Make) are detected for future use but skipped — adding
    runner-specific invocation + parsing logic each is its own
    point release. v1.27.x can incrementally add them.

  * Default ON; opt-out via ``JANUS_AUTO_VERIFY=0``. Default timeout
    30s; configurable via ``JANUS_VERIFY_TIMEOUT``. Tests bootstrap
    with verification OFF in conftest.py so the suite doesn't run
    pytest against itself recursively.

  * Skips refusal/error tool results — no point verifying when the
    edit didn't happen.

  * Skips edits to test files themselves only when there's no
    further test file pointing at them. If the edited file is
    ``tests/test_foo.py``, run that file directly.

  * Falls back gracefully: no targeted test → no verification (no
    log spam, no model context bloat). The model can run pytest
    manually if it wants the full suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


# ---------- Constants ----------

CODE_EXTENSIONS = frozenset({".py"})  # v1.27.1: Python only
CODE_EDITING_TOOLS = frozenset({"fs_write", "fs_edit", "fs_multi_edit"})

DEFAULT_TIMEOUT = 30  # seconds
MAX_OUTPUT = 2500     # chars of verification output we paste into context

# Common test directory names — checked in order; first match wins
# when a project nests tests under one of these.
TEST_DIRS = ("tests", "test")


# ---------- Detection helpers ----------


def is_code_edit(tool_name: str, args: dict | None) -> bool:
    """Was this tool call a code edit we should verify?"""
    if tool_name not in CODE_EDITING_TOOLS:
        return False
    args = args or {}
    path = args.get("path") or ""
    if not path:
        return False
    return Path(path).suffix.lower() in CODE_EXTENSIONS


def is_python_project(workspace: Path) -> bool:
    """Heuristic: is this workspace a Python project?

    Looks for pyproject.toml / setup.py / setup.cfg / a tests dir.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        return False
    if (workspace / "pyproject.toml").exists():
        return True
    if (workspace / "setup.py").exists():
        return True
    if (workspace / "setup.cfg").exists():
        return True
    for d in TEST_DIRS:
        if (workspace / d).is_dir():
            return True
    return False


def find_test_targets(workspace: Path, edited_path: str) -> list[Path]:
    """Find pytest target files for a given edited source path.

    Conventions tried (in order):
      * The edited file ITSELF if it's a test_*.py (the change to a
        test file is the test).
      * ``tests/test_<stem>.py``
      * ``tests/<...>/test_<stem>.py`` — recursive search under tests
      * ``test_<stem>.py`` next to the edited source (rare but valid)

    Returns absolute resolved paths. Empty list = no target found =
    skip verification.
    """
    workspace = Path(workspace).resolve()
    edited = Path(edited_path)

    # Resolve to absolute relative to workspace
    edited_abs = edited if edited.is_absolute() else (workspace / edited)
    try:
        edited_abs = edited_abs.resolve()
    except (OSError, RuntimeError):
        return []

    targets: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        if p.exists() and p.is_file() and p.suffix == ".py":
            try:
                rp = p.resolve()
            except (OSError, RuntimeError):
                return
            if rp not in seen:
                seen.add(rp)
                targets.append(rp)

    # 0. Edit IS a test file → run that file
    if edited.name.startswith("test_") and edited.suffix == ".py":
        _add(edited_abs)

    # 1. tests/test_<stem>.py and test/test_<stem>.py
    stem = edited.stem
    if stem and not stem.startswith("test_"):
        for d in TEST_DIRS:
            _add(workspace / d / f"test_{stem}.py")

    # 2. test_<stem>.py next to source
    if stem and not stem.startswith("test_"):
        try:
            sibling = edited_abs.parent / f"test_{stem}.py"
            _add(sibling)
        except (OSError, RuntimeError):
            pass

    # 3. Recursive search under tests/
    if stem and not stem.startswith("test_"):
        for d in TEST_DIRS:
            tdir = workspace / d
            if tdir.is_dir():
                try:
                    for p in tdir.rglob(f"test_{stem}.py"):
                        _add(p)
                except (OSError, RuntimeError):
                    continue

    return targets


# ---------- Run + format ----------


def _relpath(workspace: Path, abs_path: Path) -> str:
    """Best-effort relative path; absolute fallback."""
    try:
        return str(abs_path.resolve().relative_to(workspace.resolve()))
    except (OSError, ValueError, RuntimeError):
        return str(abs_path)


def verify_python(
    workspace: Path,
    edited_path: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Run targeted pytest. Return result dict or None if skipped.

    Returns None when:
      * pytest is not installed
      * No targeted test file found
    """
    workspace = Path(workspace)
    targets = find_test_targets(workspace, edited_path)
    if not targets:
        return None

    if not shutil.which("pytest"):
        return None

    rels = [_relpath(workspace, t) for t in targets]
    cmd = ["pytest", "-x", "-q", "--tb=line", "--no-header", *rels]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        return {
            "passed": proc.returncode == 0,
            "exit_code": proc.returncode,
            "runner": "pytest",
            "targets": rels,
            "output_preview": combined[-MAX_OUTPUT:].strip(),
            "timed_out": False,
            "edited_path": edited_path,
        }
    except subprocess.TimeoutExpired as e:
        partial = ""
        if e.stdout:
            partial += (
                e.stdout.decode(errors="replace")
                if isinstance(e.stdout, bytes) else e.stdout
            )
        if e.stderr:
            partial += (
                e.stderr.decode(errors="replace")
                if isinstance(e.stderr, bytes) else e.stderr
            )
        return {
            "passed": False,
            "exit_code": -1,
            "runner": "pytest",
            "targets": rels,
            "output_preview": partial[-MAX_OUTPUT:].strip(),
            "timed_out": True,
            "edited_path": edited_path,
        }
    except Exception as e:
        return {
            "passed": False,
            "exit_code": -1,
            "runner": "pytest",
            "targets": rels,
            "output_preview": (
                f"verification crashed: {type(e).__name__}: {e}"
            ),
            "timed_out": False,
            "edited_path": edited_path,
        }


def format_result(result: dict) -> str:
    """Render the verification result as a Markdown block for the model.

    Compact on success (one summary line); expanded on failure (full
    output preview so the model sees the assertion error).
    """
    label = result.get("runner", "verify")
    targets = result.get("targets") or []
    targets_str = ", ".join(targets)
    if result.get("timed_out"):
        timeout = os.environ.get(
            "JANUS_VERIFY_TIMEOUT", str(DEFAULT_TIMEOUT),
        )
        return (
            f"[verification: {label} {targets_str}] TIMED OUT "
            f"after {timeout}s\n"
            f"{result.get('output_preview', '')}"
        ).strip()
    if result.get("passed"):
        out = (result.get("output_preview") or "").strip()
        last_line = out.splitlines()[-1] if out else "ok"
        return (
            f"[verification: {label} {targets_str}] PASSED — {last_line}"
        ).strip()
    return (
        f"[verification: {label} {targets_str}] FAILED "
        f"(exit {result.get('exit_code', -1)})\n"
        f"{result.get('output_preview', '')}"
    ).strip()


# ---------- Public hook for executor.chat ----------


def maybe_verify(
    tool_name: str,
    args: dict | None,
    result: str,
    *,
    workspace: str | Path,
) -> Optional[dict]:
    """Run verification if applicable. Return result dict or None.

    Conditions checked (cheap → expensive):
      1. ``JANUS_AUTO_VERIFY`` env var (off by default in tests).
      2. Tool is fs_write/fs_edit/fs_multi_edit on a .py file.
      3. Tool result wasn't an error/refusal.
      4. Workspace looks like a Python project.
      5. A targeted test file exists.
      6. pytest is installed.

    Any failure short-circuits to None. Caller pastes
    ``format_result(maybe_verify(...))`` into the model context.
    """
    if os.environ.get("JANUS_AUTO_VERIFY", "1") == "0":
        return None
    if not is_code_edit(tool_name, args):
        return None
    if isinstance(result, str) and result.startswith(("error:", "refused")):
        return None

    workspace = Path(workspace)
    if not is_python_project(workspace):
        return None

    args = args or {}
    path = args.get("path") or ""
    if not path:
        return None

    try:
        timeout = int(os.environ.get("JANUS_VERIFY_TIMEOUT") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    return verify_python(workspace, path, timeout=timeout)


__all__ = [
    "CODE_EXTENSIONS",
    "CODE_EDITING_TOOLS",
    "DEFAULT_TIMEOUT",
    "MAX_OUTPUT",
    "is_code_edit",
    "is_python_project",
    "find_test_targets",
    "verify_python",
    "format_result",
    "maybe_verify",
]
