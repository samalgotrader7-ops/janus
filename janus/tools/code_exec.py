"""
tools/code_exec.py — Phase 9: sandboxed Python in a subprocess.

DEFENSE LAYERS (each independently bypassable, all together adequate):

1. AST pre-flight rejects:
   - imports from a deny-list of `os`, `subprocess`, `socket`, `pathlib`,
     `ctypes`, `multiprocessing`, `threading`, `importlib`, `pickle`,
     `marshal`, `urllib`, `http`, `ftplib`, `smtplib`, `pty`, `resource`,
     `fcntl`, `compileall`, `code`, `shutil`, `site`, `platform`.
   - calls to `eval`, `exec`, `compile`, `__import__`, `open`.
   - dunder-attribute access patterns commonly used for sandbox escape:
     `__class__`, `__bases__`, `__subclasses__`, `__globals__`,
     `__import__`, `__builtins__`, `__getattribute__`.

2. Python invoked with `-I` (isolated mode): no PYTHON* env vars,
   site-packages disabled, user site disabled, USERBASE disabled.

3. Subprocess env stripped to `PATH` + `PYTHONIOENCODING` only — no
   credentials leak via env, no JANUS_* exposure.

4. cwd pinned to WORKSPACE; relative file ops cannot escape.

5. Wall-clock timeout via subprocess timeout (default 10s, capped 30s).

6. Output truncated to 50KB.

THIS IS NOT A FULL SANDBOX. It is "raises the bar" sandboxing that closes
the well-known Hermes execute_code escapes (Issue #41 PYTHONPATH; Issue
#7071 internal-module discovery). Phase 12 may strengthen further.
"""

from __future__ import annotations
import ast
import os
import subprocess
import sys
from typing import Callable

from . import base
from .. import config


_FORBIDDEN_IMPORTS = frozenset({
    "os", "sys", "subprocess", "socket", "pathlib", "ctypes",
    "multiprocessing", "threading", "asyncio",
    "importlib", "pickle", "marshal", "fcntl", "resource",
    "urllib", "http", "ftplib", "smtplib", "telnetlib",
    "platform", "site", "code", "compileall", "pty",
    "shutil", "tempfile", "_thread", "signal", "atexit",
})

_FORBIDDEN_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "open",
})

_DANGEROUS_DUNDERS = frozenset({
    "__class__", "__bases__", "__subclasses__", "__mro__",
    "__globals__", "__import__", "__builtins__",
    "__getattribute__", "__getattr__", "__setattr__",
    "__delattr__", "__init_subclass__",
})


def ast_check(code: str) -> str | None:
    """Return None if code passes the deny-list, else a violation reason."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax error: {e.msg} (line {e.lineno})"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_IMPORTS:
                    return f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in _FORBIDDEN_IMPORTS:
                return f"forbidden import from: {node.module}"
        elif isinstance(node, ast.Call):
            target = None
            if isinstance(node.func, ast.Name):
                target = node.func.id
            elif isinstance(node.func, ast.Attribute):
                target = node.func.attr
            if target in _FORBIDDEN_CALLS:
                return f"forbidden call: {target}()"
        elif isinstance(node, ast.Attribute):
            if node.attr in _DANGEROUS_DUNDERS:
                return f"forbidden attribute access: .{node.attr}"
        elif isinstance(node, ast.Name):
            if node.id in _DANGEROUS_DUNDERS:
                return f"forbidden name reference: {node.id}"
    return None


class CodeExecPython(base.Tool):
    name = "code_exec_python"
    description = (
        "Execute a Python snippet in an isolated subprocess. "
        "NO network, NO os/subprocess/socket, NO file open, NO eval/exec. "
        "AST pre-flight refuses common escapes (PYTHONPATH, dunder walks, "
        "import-of-os via __builtins__). cwd is pinned to the workspace. "
        "Time-bounded (default 10s, max 30s). For arithmetic, data "
        "transforms, JSON parsing, regex — NOT for I/O."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute."},
            "timeout": {
                "type": "integer",
                "description": f"Wall-clock seconds (default "
                               f"{config.CODE_EXEC_TIMEOUT_DEFAULT}, max "
                               f"{config.CODE_EXEC_TIMEOUT_MAX}).",
            },
        },
        "required": ["code"],
    }
    dangerous = True

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        code = str(args.get("code") or "")
        if not code.strip():
            return "error: code is required"

        timeout = min(
            int(args.get("timeout") or config.CODE_EXEC_TIMEOUT_DEFAULT),
            config.CODE_EXEC_TIMEOUT_MAX,
        )

        violation = ast_check(code)
        if violation:
            return f"refused (AST pre-flight): {violation}"

        details = (
            f"python ({len(code)} chars), timeout {timeout}s, "
            f"isolated subprocess in {config.WORKSPACE}"
        )
        if not approver(
            "code_exec_python",
            details,
            capability=("code", "exec", "python"),
        ):
            return "refused by user: code_exec_python"

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", code],
                capture_output=True, text=True,
                timeout=timeout, env=env,
                cwd=str(config.WORKSPACE),
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"error: code timed out after {timeout}s"
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

        out = proc.stdout or ""
        if proc.stderr:
            out += "\n[stderr]\n" + proc.stderr
        if len(out) > config.CODE_EXEC_OUTPUT_BYTES:
            out = (
                out[: config.CODE_EXEC_OUTPUT_BYTES]
                + f"\n[truncated; total was {len(out)} bytes]"
            )
        return f"exit={proc.returncode}\n{out}"
