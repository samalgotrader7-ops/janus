"""
triggers/base.py — Phase 6 — trigger format and loading.

Each trigger is a YAML file at ~/.janus/triggers/<name>.yaml:

    name: morning-news
    kind: cron               # cron | file_change | log_pattern
    when: "0 7 * * *"        # cron: 5-field cron expression
                             # file_change: glob pattern (relative to workspace)
                             # log_pattern: regex matched against new log entries
    skill: morning-news      # which skill to invoke; capability tokens still apply
    request: "Send my morning brief"   # the request text passed to interpreter
    dedupe_seconds: 3600     # don't fire same trigger within this window
    enabled: true

WHY YAML:
  Same reasoning as skills — the user reads + edits these directly. No GUI.

CAPABILITY GATING:
  Triggers can ONLY invoke a named skill. They cannot run free-form requests
  with raw shell access. The skill's tokens determine what's allowed.

DEDUP:
  daemon.state.json tracks last-fired times per trigger name. The daemon
  checks dedupe_seconds before firing.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..skills import parse_frontmatter, _parse_yaml_subset  # YAML reuse


VALID_KINDS = ("cron", "file_change", "log_pattern", "interval")


@dataclass
class FireEvent:
    """A trigger that just fired. Carries the request to send to the interpreter."""
    trigger: str
    request: str
    skill: str | None
    fired_at: str
    detail: dict = field(default_factory=dict)


@dataclass
class Trigger:
    name: str
    kind: str
    when: str
    skill: str | None
    request: str
    dedupe_seconds: int = 0
    enabled: bool = True
    last_fired: str | None = None
    # v1.6: per-trigger delivery target. "log" (default) writes only to
    # ~/.janus/log.jsonl. "telegram:<chat_id>" sends each fire's output
    # to a specific chat — overrides global JANUS_TELEGRAM_CHATS env so
    # each agent can talk to its own user. Empty string = fall back to
    # legacy DAEMON_NOTIFY_GATEWAY env behavior.
    deliver_to: str = ""
    raw: dict = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> "Trigger":
        kind = str(d.get("kind", "")).strip()
        if kind not in VALID_KINDS:
            raise ValueError(f"trigger {d.get('name')!r}: bad kind {kind!r}")
        return cls(
            name=str(d.get("name") or (path.stem if path else "unnamed")),
            kind=kind,
            when=str(d.get("when", "")).strip(),
            skill=(d.get("skill") or None),
            request=str(d.get("request", "")).strip(),
            dedupe_seconds=int(d.get("dedupe_seconds") or 0),
            enabled=bool(d.get("enabled", True)),
            deliver_to=str(d.get("deliver_to") or "").strip(),
            raw=d,
            path=path,
        )


def list_triggers() -> list[Trigger]:
    config.ensure_home()
    out: list[Trigger] = []
    for p in sorted(config.TRIGGERS_DIR.glob("*.yaml")):
        try:
            text = p.read_text(encoding="utf-8")
            d = _parse_yaml_subset(text)
            t = Trigger.from_dict(d, path=p)
            out.append(t)
        except Exception:
            continue
    return out


def load_triggers() -> dict[str, Trigger]:
    return {t.name: t for t in list_triggers()}


# ---------- State (last-fired timestamps) ----------


def read_state() -> dict[str, str]:
    if not config.DAEMON_STATE.exists():
        return {}
    try:
        return json.loads(config.DAEMON_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(state: dict[str, str]) -> None:
    config.ensure_home()
    config.DAEMON_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------- Matchers ----------


def cron_due(expr: str, now_minute: tuple[int, int, int, int, int]) -> bool:
    """Tiny 5-field cron matcher: minute hour dom month dow.

    Each field accepts:
      *  any
      N  exact
      */N every-N-from-zero
      A,B,C  any of
    """
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, mon, dow = now_minute
    targets = (minute, hour, dom, mon, dow)
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    for f, t, (lo, hi) in zip(fields, targets, bounds):
        if not _cron_field_match(f, t, lo, hi):
            return False
    return True


def _cron_field_match(field: str, value: int, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            step = int(field[2:])
        except ValueError:
            return False
        return step > 0 and (value - lo) % step == 0
    parts = field.split(",")
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            return False
    return value in nums


def file_glob_changed(pattern: str, last_check_ts: float) -> tuple[bool, list[str]]:
    """Has any file matching `pattern` (under WORKSPACE) been modified since last check?"""
    matches: list[str] = []
    for p in config.WORKSPACE.glob(pattern):
        try:
            if p.stat().st_mtime > last_check_ts:
                matches.append(str(p.relative_to(config.WORKSPACE)))
        except OSError:
            continue
    return bool(matches), matches


def log_pattern_match(regex: str, recent_lines: list[str]) -> list[str]:
    """Return lines from recent_lines that match the regex."""
    try:
        rx = re.compile(regex)
    except re.error:
        return []
    return [ln for ln in recent_lines if rx.search(ln)]


def interval_due(seconds_str: str, last_fired_iso: str | None, now_iso: str) -> bool:
    """`when: "300"` fires every 300 seconds."""
    try:
        secs = int(seconds_str)
    except ValueError:
        return False
    if not last_fired_iso:
        return True
    import datetime as dt
    try:
        last = dt.datetime.fromisoformat(last_fired_iso)
        now = dt.datetime.fromisoformat(now_iso)
    except ValueError:
        return True
    return (now - last).total_seconds() >= secs
