"""
prune_cron.py — daemon-managed periodic pruning (v1.43.0).

Both memory_prune and skill_prune are pure-compute (no LLM calls), so the
daemon can run them on a fixed cadence without surprising the user with
token spend — unlike memory_consolidate, which DOES burn tokens and so
defaults to off in v1.30.2.

Defaults: 24h cadence for both, on by default. Disable individually with
``JANUS_MEMORY_PRUNE_HOURS=0`` / ``JANUS_SKILL_PRUNE_HOURS=0``.

State persists across daemon restarts via ``~/.janus/prune_state.json`` so
a crash + 30s restart doesn't double-fire the prune (which is otherwise
idempotent anyway — the rules check age windows — but burning a re-scan
on every restart is wasteful).

This module is identical in shape to memory_consolidate_cron so the two
ticks are interchangeable from the daemon's perspective.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from typing import Optional

from . import config


def state_path() -> Path:
    return config.HOME / "prune_state.json"


def _now() -> float:
    return time.time()


def read_state() -> dict:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        pass


def should_run_now(
    state: dict,
    *,
    key: str,
    hours: int,
    now: Optional[float] = None,
) -> bool:
    if hours <= 0:
        return False
    if now is None:
        now = _now()
    last = state.get(key)
    if not isinstance(last, (int, float)):
        return True
    return (now - float(last)) >= hours * 3600


def _iso(now: float) -> str:
    return dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).isoformat(
        timespec="seconds"
    )


def tick_memory(
    *,
    hours: Optional[int] = None,
    now: Optional[float] = None,
    on_fire=None,
) -> dict:
    """Fire memory_prune.run_once if due. Returns status dict.

    ``hours <= 0`` returns ``{"fired": False, "reason": "disabled"}``.
    """
    if hours is None:
        hours = config.MEMORY_PRUNE_HOURS
    if now is None:
        now = _now()

    if hours <= 0:
        return {"fired": False, "reason": "disabled"}

    state = read_state()
    if not should_run_now(state, key="last_memory_prune_ts", hours=hours, now=now):
        return {"fired": False, "reason": "not_due"}

    # Mark BEFORE running so a hang doesn't replay on next tick.
    state["last_memory_prune_ts"] = now
    state["last_memory_prune_iso"] = _iso(now)
    state["memory_prune_runs"] = int(state.get("memory_prune_runs", 0)) + 1
    state["last_memory_prune_error"] = ""
    write_state(state)

    if on_fire is not None:
        try:
            on_fire()
        except Exception:
            pass

    try:
        from . import memory_prune
        counts = memory_prune.run_once(now=dt.datetime.fromtimestamp(
            now, tz=dt.timezone.utc
        ))
        state["last_memory_prune_counts"] = counts
        write_state(state)
        return {"fired": True, **counts}
    except Exception as e:
        state["last_memory_prune_error"] = str(e)[:500]
        write_state(state)
        return {"fired": True, "error": str(e)}


def tick_skill(
    *,
    hours: Optional[int] = None,
    now: Optional[float] = None,
    on_fire=None,
) -> dict:
    """Fire skill_prune.run_once if due. Returns status dict."""
    if hours is None:
        hours = config.SKILL_PRUNE_HOURS
    if now is None:
        now = _now()

    if hours <= 0:
        return {"fired": False, "reason": "disabled"}

    state = read_state()
    if not should_run_now(state, key="last_skill_prune_ts", hours=hours, now=now):
        return {"fired": False, "reason": "not_due"}

    state["last_skill_prune_ts"] = now
    state["last_skill_prune_iso"] = _iso(now)
    state["skill_prune_runs"] = int(state.get("skill_prune_runs", 0)) + 1
    state["last_skill_prune_error"] = ""
    write_state(state)

    if on_fire is not None:
        try:
            on_fire()
        except Exception:
            pass

    try:
        from . import skill_prune
        counts = skill_prune.run_once(now=dt.datetime.fromtimestamp(
            now, tz=dt.timezone.utc
        ))
        state["last_skill_prune_counts"] = counts
        write_state(state)
        return {"fired": True, **counts}
    except Exception as e:
        state["last_skill_prune_error"] = str(e)[:500]
        write_state(state)
        return {"fired": True, "error": str(e)}


__all__ = [
    "state_path",
    "read_state",
    "write_state",
    "should_run_now",
    "tick_memory",
    "tick_skill",
]
