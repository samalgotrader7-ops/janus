"""
doctor.py — `/doctor`: self-diagnose install + configuration (Phase 15).

Twelve checks run in parallel-friendly order: cheapest first, then
network checks. Each returns a CheckResult with status (pass/warn/fail),
a one-line message, and an optional remediation hint.

NEVER RAISES (P8): a failing check returns `fail`, never crashes.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import config


@dataclass
class CheckResult:
    name: str
    status: str        # "pass" | "warn" | "fail"
    message: str
    fix: str = ""


def run_all() -> list[CheckResult]:
    return [
        _check_api_key(),
        _check_workspace(),
        _check_home(),
        _check_log(),
        _check_skills_dir(),
        _check_user_md(),
        _check_optional_dep("rich", "rich"),
        _check_optional_dep("prompt_toolkit", "prompt_toolkit"),
        _check_optional_dep("fastapi", "fastapi"),
        _check_optional_dep("playwright", "playwright"),
        _check_optional_dep("telegram", "python-telegram-bot"),
        _check_mcp(),
    ]


def render(results: Iterable[CheckResult], color: bool = True) -> str:
    icons = {"pass": "✓", "warn": "⚠", "fail": "✗"}
    if color:
        colors = {"pass": "\033[32m", "warn": "\033[33m", "fail": "\033[31m"}
        reset = "\033[0m"
    else:
        colors = {k: "" for k in icons}; reset = ""
    lines = []
    for r in results:
        c = colors.get(r.status, "")
        lines.append(
            f"  {c}{icons.get(r.status, '?')}{reset} "
            f"[{r.status:5}] {r.name:<22} {r.message}"
        )
        if r.fix:
            lines.append(f"           {' '*22}  fix: {r.fix}")
    counts = _summary(results)
    lines.append("")
    lines.append(f"  {counts['pass']} pass · {counts['warn']} warn · {counts['fail']} fail")
    return "\n".join(lines)


def _summary(results: Iterable[CheckResult]) -> dict[str, int]:
    out = {"pass": 0, "warn": 0, "fail": 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


# ---------- individual checks ----------


def _check_api_key() -> CheckResult:
    if config.API_KEY:
        return CheckResult("API key", "pass", f"set ({len(config.API_KEY)} chars)")
    return CheckResult(
        "API key", "fail", "JANUS_API_KEY not set",
        fix="set JANUS_API_KEY in env or .env",
    )


def _check_home() -> CheckResult:
    if not config.HOME.exists():
        return CheckResult(
            "home dir", "warn", f"{config.HOME} missing (will be created on first use)",
        )
    if not os.access(config.HOME, os.W_OK):
        return CheckResult(
            "home dir", "fail", f"{config.HOME} not writable",
            fix="check filesystem permissions",
        )
    return CheckResult("home dir", "pass", str(config.HOME))


def _check_workspace() -> CheckResult:
    p = Path(config.WORKSPACE)
    if not p.exists():
        return CheckResult("workspace", "fail", f"{p} missing",
                           fix="set JANUS_WORKSPACE to a real directory")
    if not p.is_dir():
        return CheckResult("workspace", "fail", f"{p} is not a directory")
    return CheckResult("workspace", "pass", str(p))


def _check_log() -> CheckResult:
    if not config.LOG_FILE.exists():
        return CheckResult("log.jsonl", "warn", "empty (no interactions yet)")
    try:
        n = sum(1 for _ in config.LOG_FILE.open(encoding="utf-8"))
    except Exception as e:
        return CheckResult("log.jsonl", "fail", f"unreadable: {e}")
    return CheckResult("log.jsonl", "pass", f"{n} entries")


def _check_skills_dir() -> CheckResult:
    if not config.SKILLS_DIR.is_dir():
        return CheckResult("skills/", "warn", "missing — try /skill new")
    n = len(list(config.SKILLS_DIR.glob("*.md")))
    if n == 0:
        return CheckResult("skills/", "warn", "0 skills",
                           fix="run /skill new or /skill import <path>")
    return CheckResult("skills/", "pass", f"{n} skill(s)")


def _check_user_md() -> CheckResult:
    if not config.USER_MODEL_FILE.exists():
        return CheckResult(
            "user.md", "warn", "not yet present",
            fix="try /init to draft one from your workspace",
        )
    sz = config.USER_MODEL_FILE.stat().st_size
    return CheckResult("user.md", "pass", f"{sz} bytes")


def _check_optional_dep(import_name: str, pkg_name: str) -> CheckResult:
    try:
        __import__(import_name)
        return CheckResult(f"dep:{pkg_name}", "pass", "installed")
    except ImportError:
        return CheckResult(
            f"dep:{pkg_name}", "warn", "not installed",
            fix=f"pip install {pkg_name}",
        )


def _check_mcp() -> CheckResult:
    try:
        from .mcp.client import load_servers, get_active_clients
    except Exception as e:
        return CheckResult("mcp", "fail", f"module load failed: {e}")
    servers = load_servers()
    active = get_active_clients()
    if not servers and not active:
        return CheckResult(
            "mcp servers", "warn", "none configured",
            fix=f"drop a JSON config at {config.MCP_SERVERS_FILE} "
                f"or use ~/.claude/settings.json",
        )
    return CheckResult(
        "mcp servers", "pass",
        f"{len(servers)} configured, {len(active)} connected",
    )
