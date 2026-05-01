"""Tests for Phase 12 — cache snapshot + code_exec escape resistance.

Two concerns:
1. `cache.snapshot()` captures the current preamble and is reusable
   across turns; refreshing must visibly change the captured value.
2. `code_exec_python` / `ast_check` resist a battery of known sandbox
   escape patterns. Each pattern listed below is from a real Hermes /
   Python sandbox bypass writeup.
"""
from __future__ import annotations

import pytest

from janus import cache, config, memory
from janus.tools.code_exec import ast_check, CodeExecPython


# ---------- cache.snapshot ----------


def test_snapshot_picks_up_current_user_md(janus_home):
    config.USER_MODEL_FILE.write_text(
        "# user.md\n\n## Identity\nSam — solo dev.\n",
        encoding="utf-8",
    )
    snap = cache.snapshot()
    assert "Sam — solo dev." in snap.preamble
    assert len(snap) > 0


def test_snapshot_empty_when_no_user_md(janus_home):
    # janus_home fixture creates fresh tmpdir; user.md absent.
    snap = cache.snapshot()
    assert snap.preamble == ""


def test_snapshot_does_not_change_until_explicitly_refreshed(janus_home):
    config.USER_MODEL_FILE.write_text(
        "# user.md\n\n## A\nfirst\n", encoding="utf-8",
    )
    snap = cache.snapshot()
    first = snap.preamble

    # Mutate user.md.
    config.USER_MODEL_FILE.write_text(
        "# user.md\n\n## A\nSECOND\n", encoding="utf-8",
    )
    # Snapshot is stale — that's the point. Caller controls when to refresh.
    assert snap.preamble == first

    snap2 = cache.snapshot()
    assert snap2.preamble != first
    assert "SECOND" in snap2.preamble


def test_two_snapshots_within_one_turn_are_byte_identical(janus_home):
    """The cache hit guarantee: two snapshots taken back-to-back without
    any user.md mutation produce strings that compare equal."""
    config.USER_MODEL_FILE.write_text(
        "# user.md\n\n## A\nstable content\n", encoding="utf-8",
    )
    a = cache.snapshot().preamble
    b = cache.snapshot().preamble
    assert a == b


# ---------- code_exec escape patterns ----------


# Each entry: (description, code, expected_violation_substring)
_ESCAPE_PATTERNS = [
    ("direct __import__", "__import__('os')", "__import__"),
    ("import os", "import os", "os"),
    ("import sys", "import sys", "sys"),
    ("from os import path", "from os import path", "os"),
    ("submodule import", "import os.path as p", "os"),
    ("import subprocess", "import subprocess", "subprocess"),
    ("import socket", "import socket", "socket"),
    ("import ctypes", "import ctypes", "ctypes"),
    ("import pickle", "import pickle", "pickle"),
    ("import urllib", "import urllib.request", "urllib"),

    ("eval call", "eval('1+1')", "eval"),
    ("exec call", "exec('x=1')", "exec"),
    ("compile call", "compile('print(1)', '', 'exec')", "compile"),
    ("open call", "open('/etc/passwd')", "open"),

    ("__class__ access", "''.__class__", "__class__"),
    ("__bases__ access", "().__class__.__bases__", "__"),
    ("__subclasses__", "().__class__.__bases__[0].__subclasses__()", "__"),
    ("__mro__", "().__class__.__mro__", "__mro__"),
    ("__globals__", "(lambda: 0).__globals__", "__globals__"),
    ("__builtins__ name", "__builtins__['eval']('1+1')", "__builtins__"),
    ("__getattribute__", "''.__getattribute__('upper')", "__getattribute__"),
]


@pytest.mark.parametrize("desc,code,expected", _ESCAPE_PATTERNS,
                         ids=[d for d, _, _ in _ESCAPE_PATTERNS])
def test_ast_check_blocks_escape(desc, code, expected):
    violation = ast_check(code)
    assert violation is not None, f"{desc!r}: {code!r} was NOT blocked"
    assert expected in violation, (
        f"{desc!r}: violation message missing {expected!r}: {violation!r}"
    )


def test_ast_check_accepts_pure_arithmetic():
    assert ast_check("x = sum(range(100)); print(x)") is None


def test_ast_check_accepts_json_round_trip():
    # `import json` is NOT in the deny list — pure-Python stdlib parsing
    # is fine; it doesn't open files or sockets.
    assert ast_check("import json; print(json.dumps({'a': 1}))") is None


def test_ast_check_accepts_math():
    assert ast_check("import math; print(math.sqrt(2))") is None


def test_code_exec_blocks_pythonpath_injection_at_subprocess(janus_home, monkeypatch):
    """Even if the AST allowed something, the subprocess uses `python -I`
    (isolated mode) which neutralizes PYTHONPATH. This test asserts the
    subprocess command line includes -I."""
    import subprocess as real_sp
    captured: dict = {}

    real_run = real_sp.run

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env", {})
        # Don't actually run; return a fake CompletedProcess.
        class _R:
            returncode = 0
            stdout = "ok\n"
            stderr = ""
        return _R()

    monkeypatch.setattr(real_sp, "run", fake_run)
    out = CodeExecPython().run(
        {"code": "x = 1"},
        lambda *a, **kw: True,
    )
    # `python -I -c <code>` was the command line.
    assert captured["cmd"][1] == "-I", f"missing -I flag: {captured['cmd']}"
    # JANUS_API_KEY etc. NOT in the subprocess env (only PATH + PYTHONIOENCODING).
    assert "JANUS_API_KEY" not in captured["env"]
    assert "PYTHONPATH" not in captured["env"]
    assert "ok" in out
