"""
audit_log.py — production audit trail (v1.33.5, Phase 6.6).

WHY THIS EXISTS:
Phase 6 / Production hardening. Janus's web gateway already
records auth + mutate events to ~/.janus/web_audit.jsonl
(gateways/web_audit.py). Pre-v1.33.5 the OTHER mutations — mode
changes, memory edits, skill promotions, MCP connects — were not
audited. Production deployments need a single tail-friendly file
to grep when "what changed in the last hour?" comes up.

This module ships ~/.janus/audit.jsonl with one record per event.
Format mirrors web_audit.jsonl (ISO timestamp + action + fields)
so operators learn one schema for both files.

USAGE:
  from janus import audit_log
  audit_log.record("skill.promote", name="git-pr-review", from_state="quarantined", to_state="trusted")
  audit_log.record("mcp.connect", server="filesystem")

  # CLI:
  janus audit                       # tail latest 100 events
  janus audit --tail 500            # tail latest N
  janus audit --action skill.promote
  janus audit --since 2026-05-10    # ISO date filter

FAILURE SEMANTICS:
Append failures are SILENT — auditing must never break the
parent path. Operators monitor disk space + file presence
themselves; we don't add another error path through tooling.

REDACTION:
Caller chooses what to log. Don't pass secrets / API keys / user
content; we don't try to scrub. The web_audit pattern (log
metadata, not content) applies here too.

P5 (plain-text state): the file is a standard JSONL. tail / grep /
jq / cat all work. No compression, no rotation (operators handle
rotation via logrotate or similar — backup excluded by default).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from . import config


_LOCK = threading.Lock()


def _audit_path() -> Path:
    return Path(config.HOME) / "audit.jsonl"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record(action: str, **details: Any) -> None:
    """Append one audit event. Failure-silent.

    Standard actions used by the codebase:
      mode.change         { from_mode, to_mode }
      memory.apply        { card, op, lines_added, lines_removed }
      skill.promote       { name, from_state, to_state }
      skill.demote        { name, from_state, to_state }
      mcp.connect         { server }
      mcp.disconnect      { server }
      audit.start         { reason }
      backup.create       { archive_path, file_count }
      backup.restore      { archive_path, file_count }
    """
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record_obj = {
            "ts": _now_iso(),
            "action": str(action),
            "details": dict(details),
        }
        line = json.dumps(record_obj, default=str)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
    except Exception:
        # Never break the parent path on audit failure.
        pass


def read_lines(*, max_lines: int | None = None) -> list[dict[str, Any]]:
    """Return the most recent `max_lines` audit records (or all
    when max_lines is None). Empty list when the file doesn't
    exist."""
    path = _audit_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if max_lines is not None and len(out) > max_lines:
        out = out[-max_lines:]
    return out


def filter_records(
    records: list[dict[str, Any]],
    *,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield records matching the filters. ISO timestamps are
    compared lexicographically (correct because Z-suffixed UTC).

    `action` may be a literal action name OR a prefix ending in '.'
    (e.g., 'skill.' matches both skill.promote and skill.demote).
    """
    for r in records:
        if action:
            r_action = str(r.get("action", ""))
            if action.endswith("."):
                if not r_action.startswith(action):
                    continue
            elif r_action != action:
                continue
        ts = str(r.get("ts", ""))
        if since and ts < since:
            continue
        if until and ts > until:
            continue
        yield r


# ---------- CLI dispatch ----------


def cmd_audit(args: list[str]) -> int:
    """`janus audit [--tail N] [--action X] [--since DATE] [--until DATE]`"""
    tail = 100
    action = None
    since = None
    until = None

    i = 0
    while i < len(args):
        flag = args[i]
        if flag == "--tail":
            try:
                tail = int(args[i + 1])
                i += 2
            except (IndexError, ValueError):
                sys.stderr.write("error: --tail requires an integer\n")
                return 2
        elif flag == "--action":
            try:
                action = args[i + 1]
                i += 2
            except IndexError:
                sys.stderr.write("error: --action requires a value\n")
                return 2
        elif flag == "--since":
            try:
                since = args[i + 1]
                i += 2
            except IndexError:
                sys.stderr.write("error: --since requires an ISO date\n")
                return 2
        elif flag == "--until":
            try:
                until = args[i + 1]
                i += 2
            except IndexError:
                sys.stderr.write("error: --until requires an ISO date\n")
                return 2
        elif flag in ("-h", "--help"):
            sys.stdout.write(
                "usage: janus audit [--tail N] [--action X] "
                "[--since DATE] [--until DATE]\n"
                "  Filter and print ~/.janus/audit.jsonl. Default tail=100.\n"
                "  Action can be a literal name (skill.promote) or a "
                "prefix ending in '.' (skill. matches all).\n"
            )
            return 0
        else:
            sys.stderr.write(f"error: unknown flag {flag!r}\n")
            return 2

    records = read_lines()
    filtered = list(filter_records(records, action=action, since=since, until=until))
    if tail is not None and len(filtered) > tail:
        filtered = filtered[-tail:]

    if not filtered:
        sys.stdout.write("(no matching audit records)\n")
        return 0

    for r in filtered:
        ts = r.get("ts", "?")
        action_str = r.get("action", "?")
        details = r.get("details", {})
        details_pairs = " ".join(f"{k}={v}" for k, v in details.items())
        sys.stdout.write(f"{ts}  {action_str}  {details_pairs}\n")
    return 0
