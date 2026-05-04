"""
tool_guardrails.py — pre-flight pattern checks on dangerous tool args
(v1.10.0, Tier A item 5).

WHY THIS EXISTS:
We already have two layers of protection:
  1) Capability tokens — skill-grants short-circuit to ALLOW.
  2) Auto-mode risk patterns — auto_mode.py BLOCKS rm -rf /, fs writes
     to /etc/, SSRF fetches.

Guardrails fill the middle: BORDERLINE shapes that aren't worth blocking
outright but the user should know about. Things like:

  - fs_write to a path that already exists with substantial content
    (we'd be overwriting, not creating)
  - shell command starting with `git push --force`, `kubectl delete`,
    `terraform destroy` — destructive but legitimate
  - web_fetch to a URL > 10 MB content-length expected
  - fs_edit / fs_multi_edit on a file with uncommitted git changes

These are not auto-mode blocks (the user might want them — they're
dual-use). They're WARNINGS the tool path surfaces back to the model
as observation BEFORE running, so the model can reconsider or proceed
with full context.

DESIGN — OBSERVATION NOT INTERVENTION:
Guardrails do NOT call the approver, do NOT change the action, do NOT
mutate state. They run BEFORE the tool's run() and produce a string the
tool's run() prepends to its output. Two reasons:
  - Keeps the tool path the source of truth. Guardrails can't accidentally
    block when the user explicitly wanted the action.
  - The model gets the warning AND the result in one observation. It
    can decide whether to undo, alert the user, or proceed.

USE FROM TOOL CODE:
A tool calls `check(tool_name, args)` at the top of run(). If it returns
a non-empty string, that string is prepended to the tool's success
output. Tools opt in — guardrails are NOT auto-applied to every tool.

P8 (errors are observations): a guardrail warning is just a richer
observation. The model reads it like any other tool result.
"""

from __future__ import annotations
import re
from pathlib import Path

from . import config


# Hard cap for "substantial content" — anything bigger than this in an
# overwrite triggers the warning.
OVERWRITE_WARN_BYTES = 100 * 1024  # 100 KB

# Shell commands that are legitimate but commonly cause regret.
_SHELL_REGRET_PATTERNS = (
    (r"\bgit\s+push\s+(?:--?force|--?force-with-lease|-f)\b",
     "git force-push"),
    (r"\bgit\s+reset\s+--hard\b",
     "git reset --hard (loses uncommitted work)"),
    (r"\bkubectl\s+delete\b",
     "kubectl delete (cluster mutation)"),
    (r"\bterraform\s+destroy\b",
     "terraform destroy"),
    (r"\bdocker\s+system\s+prune\b",
     "docker system prune"),
    (r"\bnpm\s+publish\b",
     "npm publish (releases to registry)"),
    (r"\bpip\s+uninstall\b",
     "pip uninstall (irreversible without reinstall)"),
    (r"\brm\s+-rf?\s+(?:\$|~|/)",
     "rm -rf with variable / home / root path"),
    (r">\s*/dev/(?:sd|nvme)",
     "raw write to block device"),
)


def check(tool_name: str, args: dict) -> str:
    """Pre-flight check. Returns a one-line warning string when the call
    is borderline, empty string otherwise.

    Multiple warnings are joined with ' · ' so the model sees them all
    on one line. Any internal exception swallowed — guardrails NEVER
    crash the tool path (P8).
    """
    try:
        warnings: list[str] = []
        if tool_name == "fs_write":
            warnings.extend(_check_fs_write(args))
        elif tool_name in ("fs_edit", "fs_multi_edit"):
            warnings.extend(_check_fs_edit(args))
        elif tool_name == "shell":
            warnings.extend(_check_shell(args))
        elif tool_name == "agent_delete":
            warnings.extend(_check_agent_delete(args))
        if warnings:
            return "[guardrail] " + " · ".join(warnings)
        return ""
    except Exception:
        return ""


# ---------- Per-tool checkers ----------


def _check_fs_write(args: dict) -> list[str]:
    out: list[str] = []
    path = args.get("path")
    if not path:
        return out
    try:
        p = Path(str(path)).expanduser()
    except (TypeError, ValueError):
        return out
    if not p.is_file():
        return out  # creating new file, no warning
    try:
        size = p.stat().st_size
    except OSError:
        return out
    if size >= OVERWRITE_WARN_BYTES:
        out.append(
            f"overwriting existing file {p.name} "
            f"({_human_bytes(size)})"
        )
    return out


def _check_fs_edit(args: dict) -> list[str]:
    out: list[str] = []
    path = args.get("path")
    if not path:
        return out
    try:
        p = Path(str(path)).expanduser()
    except (TypeError, ValueError):
        return out
    if not p.is_file():
        return out
    # In a git repo + this file has uncommitted changes? Cheap check via
    # git status --porcelain, bounded to 2s. Best-effort — failure means
    # we just don't warn.
    rel = _relative_to_repo(p)
    if rel and _has_uncommitted_changes(p.parent, rel):
        out.append(f"editing {rel} which has uncommitted git changes")
    return out


def _check_shell(args: dict) -> list[str]:
    out: list[str] = []
    cmd = args.get("command") or args.get("cmd") or ""
    if not isinstance(cmd, str):
        return out
    for pattern, label in _SHELL_REGRET_PATTERNS:
        if re.search(pattern, cmd):
            out.append(f"shell uses {label}")
    return out


def _check_agent_delete(args: dict) -> list[str]:
    """Deleting an agent removes both skill + trigger. Warn that this
    is irreversible (no soft-delete / trash) so the model surfaces it
    to the user before executing."""
    name = args.get("name")
    if not name:
        return []
    return [f"deleting agent {name!r} removes skill + trigger files (irreversible)"]


# ---------- Helpers ----------


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n // 1024}KB"
    return f"{n // (1024 * 1024)}MB"


def _relative_to_repo(p: Path) -> str | None:
    """Best-effort: return p's path relative to the closest git root."""
    cur = p.parent if p.is_file() else p
    for ancestor in [cur, *cur.parents]:
        if (ancestor / ".git").exists():
            try:
                return str(p.relative_to(ancestor))
            except ValueError:
                return None
    return None


def _has_uncommitted_changes(repo_dir: Path, rel_path: str) -> bool:
    """Cheap `git status --porcelain -- <path>` check, bounded to 2s.

    Returns True iff the file shows up in porcelain output. Returns
    False on any error (git not installed, not a repo, timeout) — we'd
    rather under-warn than fail the tool.
    """
    import subprocess
    try:
        # Walk up to find the actual git root for cwd
        for ancestor in [repo_dir, *repo_dir.parents]:
            if (ancestor / ".git").exists():
                root = ancestor
                break
        else:
            return False
        r = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return bool(r.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return False
