"""Tests for v1.31.4 — file-detector tool-name allow-list.

Companion to v1.31.3's homogeneous-sequence filter. Same field-
validation context (Sam's VPS session with heavy shell usage):
``_extract_tool_calls`` falls back from ``args.path`` to
``args.command`` when the call has no path arg, so a shell tool
with command="ls" (or "pwd", "pytest", etc. — short bare commands
with no space) would slip the heuristic filters and surface as a
"File 'ls' touched N times" pattern.

DESIGN INVARIANT PINNED:
  * Only tools in ``FILE_PATH_TOOLS`` contribute to file-pattern
    counts. Adding a new file-touching tool requires updating the
    allow-list explicitly.
  * Existing string heuristics (URL, prefix, space) preserved as
    defense-in-depth — even an FILE_PATH_TOOLS member with an
    unexpected path shape gets filtered.
"""

from __future__ import annotations

from janus import skill_proposer


def _call(tool: str, path: str = "") -> dict:
    return {"tool": tool, "path": path}


# ============================================================
# The field shape: shell with bare command
# ============================================================


def test_77_shell_ls_does_not_surface_as_file():
    """The exact field shape companion: 77 ``shell`` calls with
    command="ls" (which lands in `path` via the extractor's
    fallback chain) must not surface a "File 'ls' touched 77
    times" pattern."""
    calls = [_call("shell", "ls") for _ in range(77)]
    patterns = skill_proposer._detect_repeated_files(calls)
    assert patterns == []


def test_other_bare_commands_filtered():
    """Pwd, node, pytest, make — all common bare commands."""
    for cmd in ("pwd", "node", "pytest", "make", "ls", "cargo"):
        calls = [_call("shell", cmd) for _ in range(10)]
        patterns = skill_proposer._detect_repeated_files(calls)
        assert patterns == [], (
            f"bare shell command {cmd!r} surfaced as file pattern"
        )


def test_code_exec_python_not_counted():
    """code_exec_python's `path`-shape might be Python code via
    the extractor fallback. Filter it."""
    calls = [_call("code_exec_python", "print('hi')") for _ in range(10)]
    patterns = skill_proposer._detect_repeated_files(calls)
    assert patterns == []


def test_search_queries_not_counted():
    """Web/memory/session search tools have queries, not paths."""
    for tool in ("web_search", "memory_search", "session_search"):
        calls = [_call(tool, "how to do X") for _ in range(10)]
        patterns = skill_proposer._detect_repeated_files(calls)
        assert patterns == [], (
            f"{tool} query surfaced as file pattern"
        )


# ============================================================
# Real file paths from genuine file tools still surface
# ============================================================


def test_fs_read_repeated_still_surfaces():
    calls = [_call("fs_read", "src/foo.py") for _ in range(8)]
    patterns = skill_proposer._detect_repeated_files(calls)
    assert len(patterns) == 1
    assert patterns[0].detail.get("path") == "src/foo.py"
    assert patterns[0].occurrences == 8


def test_fs_edit_repeated_still_surfaces():
    calls = [_call("fs_edit", "tests/test_foo.py") for _ in range(6)]
    patterns = skill_proposer._detect_repeated_files(calls)
    assert any(
        p.detail.get("path") == "tests/test_foo.py" for p in patterns
    )


def test_mixed_fs_tools_aggregate_per_path():
    """fs_read + fs_edit + fs_write of the same path should all
    contribute to the same file pattern (the whole point of
    "this file gets touched a lot")."""
    calls = (
        [_call("fs_read", "x.py")] * 3
        + [_call("fs_edit", "x.py")] * 2
        + [_call("fs_write", "x.py")] * 1
    )
    patterns = skill_proposer._detect_repeated_files(calls)
    matching = [p for p in patterns if p.detail.get("path") == "x.py"]
    assert matching
    assert matching[0].occurrences == 6


def test_fs_grep_path_counted():
    """fs_grep has a `path` arg pointing to the search root."""
    calls = [_call("fs_grep", "src/") for _ in range(5)]
    patterns = skill_proposer._detect_repeated_files(calls)
    assert any(p.detail.get("path") == "src/" for p in patterns)


def test_vision_path_counted():
    """vision tool reads image files — also a real file path."""
    calls = [_call("vision", "screenshots/login.png") for _ in range(5)]
    patterns = skill_proposer._detect_repeated_files(calls)
    assert any(
        p.detail.get("path") == "screenshots/login.png"
        for p in patterns
    )


# ============================================================
# Mixed workload — both noise filtered and real surfaced
# ============================================================


def test_mixed_workload_isolates_real_pattern():
    calls = []
    # 50 shell ls calls (noise — should be filtered)
    calls.extend([_call("shell", "ls") for _ in range(50)])
    # 30 web_search query calls (noise — should be filtered)
    calls.extend([_call("web_search", "python tips") for _ in range(30)])
    # 6 real fs_read of the same file (signal)
    calls.extend([_call("fs_read", "src/main.py") for _ in range(6)])

    patterns = skill_proposer._detect_repeated_files(calls)
    paths = [p.detail.get("path") for p in patterns]
    assert "src/main.py" in paths, f"real file lost in noise: {paths}"
    # No noise paths surfaced
    assert "ls" not in paths
    assert "python tips" not in paths


# ============================================================
# Defense-in-depth: heuristics still active for FILE_PATH_TOOLS
# ============================================================


def test_url_in_fs_tool_still_filtered():
    """If somehow an fs_* tool gets called with a URL-shaped path
    (shouldn't happen but defensive), the URL filter still skips it."""
    calls = [
        _call("fs_read", "https://example.com/file.py")
        for _ in range(10)
    ]
    patterns = skill_proposer._detect_repeated_files(calls)
    assert patterns == []


def test_space_in_fs_tool_path_still_filtered():
    """Defensive: a path with spaces (unusual but possible — Windows
    paths) is treated as a likely command. This is already
    pre-existing behavior; we're just confirming v1.31.4 didn't
    regress it."""
    calls = [
        _call("fs_read", "C:\\Program Files\\app\\foo.py")
        for _ in range(10)
    ]
    patterns = skill_proposer._detect_repeated_files(calls)
    # Path has space → filtered. (Note: this is an existing
    # quirk of the heuristic. Real Windows paths with spaces would
    # be missed by file detection. v1.31.4 doesn't change this.)
    assert patterns == []


# ============================================================
# Module surface
# ============================================================


def test_file_path_tools_constant_exists():
    assert hasattr(skill_proposer, "FILE_PATH_TOOLS")
    fpt = skill_proposer.FILE_PATH_TOOLS
    assert "fs_read" in fpt
    assert "fs_write" in fpt
    assert "fs_edit" in fpt
    assert "fs_multi_edit" in fpt
    # Negative — non-file tools must NOT be in the set
    assert "shell" not in fpt
    assert "web_search" not in fpt
    assert "memory_search" not in fpt
    assert "code_exec_python" not in fpt


def test_file_path_tools_is_immutable():
    """frozenset prevents accidental mutation — a mutable set
    would let a typo elsewhere silently add wrong tools."""
    assert isinstance(skill_proposer.FILE_PATH_TOOLS, frozenset)


def test_source_pin_v1_31_4_marker():
    """Marker comments preserve field-report context."""
    import inspect
    src = inspect.getsource(skill_proposer._detect_repeated_files)
    assert "v1.31.4" in src
    assert "FILE_PATH_TOOLS" in src
