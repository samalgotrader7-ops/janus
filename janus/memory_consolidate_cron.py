"""
memory_consolidate_cron.py — daemon-managed periodic consolidation
(v1.30.2).

The v1.18 design intentionally had NO built-in cron; the docstring on
``memory_consolidate.run_once`` argued users should wire ``agent_create``
for cadence because LLM calls cost money and surprising users with
silent burn was the wrong default. v1.30.2 keeps that DEFAULT (off)
but adds a built-in option for users who'd rather set an env var than
wire a meta-skill agent.

DESIGN:

  * **Opt-in via env var.** ``JANUS_MEMORY_CONSOLIDATE_HOURS`` is 0
    by default (disabled). Setting it to e.g. 24 enables a once-per-
    day cadence inside the existing daemon loop — no separate
    process, no new scheduler primitive.

  * **Pure-compute scheduler.** ``should_run_now(state, hours, now)``
    is a tiny "elapsed-since-last-run" check. State persists across
    daemon restarts via ``~/.janus/memory_consolidate_state.json``
    so a 30-min crash + restart doesn't double-fire.

  * **State file is plain JSON.** Honors P5 (plain-text persistent
    state). Manually inspectable, manually deletable when the user
    wants to force a run on next tick.

  * **Single-stage by default; multi-stage opt-in.** Mirrors the
    v1.29.0 ``run_multi_stage`` flag — set
    ``JANUS_MEMORY_CONSOLIDATE_MULTI_STAGE=1`` to use the swarm-
    shaped pipeline.

  * **Failures don't loop.** If consolidation throws, we still mark
    last_run so the next attempt waits the full interval. Otherwise
    a persistent failure (e.g., model endpoint down) would burn the
    poll loop in a tight retry.

The daemon calls ``tick()`` once per poll cycle. ``tick`` is a no-op
when consolidation is disabled or not due — cheap to call every
30 seconds.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Optional

from . import config


# Module path is module-level so tests can monkeypatch the location.
def state_path() -> Path:
    return config.HOME / "memory_consolidate_state.json"


def _now() -> float:
    return time.time()


def read_state() -> dict:
    """Read the cron state file. Returns empty dict on first run or
    on any read error — we never crash the daemon over corrupt state.

    Schema:
      {
        "last_run_ts": float,        # POSIX seconds at start of last run
        "last_run_iso": str,         # human-readable mirror
        "last_examined": int,        # cards examined on last run
        "last_written": int,         # cards written on last run
        "last_error": str,           # empty when last run succeeded
        "runs": int,                 # cumulative runs across daemon
                                     # restarts (lifetime counter)
      }
    """
    path = state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    """Persist state. Best-effort — disk-full / permission errors are
    swallowed (the daemon has more important work to do).
    """
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
    hours: int,
    now: Optional[float] = None,
) -> bool:
    """Pure-compute scheduler check.

    ``hours <= 0`` always returns False (cron disabled).
    No previous run → True (first tick after enable fires immediately).
    Otherwise, True iff at least ``hours`` have elapsed since
    ``state["last_run_ts"]``.
    """
    if hours <= 0:
        return False
    if now is None:
        now = _now()
    last = state.get("last_run_ts")
    if not isinstance(last, (int, float)):
        return True
    interval = hours * 3600
    return (now - float(last)) >= interval


def tick(
    *,
    hours: Optional[int] = None,
    multi_stage: Optional[bool] = None,
    now: Optional[float] = None,
    on_fire=None,
) -> dict:
    """One scheduler tick. Called from the daemon loop every poll.

    Returns a status dict with at least ``{"fired": bool, ...}``.
    On a fire, includes ``examined`` + ``written`` counts (or
    ``error`` on failure). On a no-op, includes ``reason`` so the
    caller can log it at high verbosity if it wants.

    ``hours`` and ``multi_stage`` default to ``config.*`` — pass
    explicitly in tests. ``on_fire`` is an optional callback the
    daemon can use to emit a print line; we don't print directly
    so tests stay silent.
    """
    if hours is None:
        hours = config.MEMORY_CONSOLIDATE_HOURS
    if multi_stage is None:
        multi_stage = config.MEMORY_CONSOLIDATE_MULTI_STAGE
    if now is None:
        now = _now()

    state = read_state()

    if hours <= 0:
        return {"fired": False, "reason": "disabled"}

    if not should_run_now(state, hours=hours, now=now):
        last = state.get("last_run_ts")
        if isinstance(last, (int, float)):
            elapsed = now - float(last)
            wait = hours * 3600 - elapsed
            return {
                "fired": False,
                "reason": "not_due",
                "wait_seconds": max(0, int(wait)),
            }
        return {"fired": False, "reason": "not_due"}

    # Fire. Mark last_run BEFORE the LLM call so a hang/crash doesn't
    # turn the next poll into another fire — but capture stats AFTER.
    state.setdefault("runs", 0)
    state["runs"] = int(state.get("runs", 0)) + 1
    state["last_run_ts"] = now
    state["last_run_iso"] = dt.datetime.fromtimestamp(
        now, tz=dt.timezone.utc,
    ).isoformat(timespec="seconds")
    state["last_error"] = ""
    write_state(state)

    if on_fire is not None:
        try:
            on_fire(multi_stage)
        except Exception:
            pass

    try:
        from . import memory_consolidate
        if multi_stage:
            result = memory_consolidate.run_multi_stage()
        else:
            result = memory_consolidate.run_once()
        examined = int(result.get("examined") or 0)
        written = int(result.get("written") or 0)
        state["last_examined"] = examined
        state["last_written"] = written
        write_state(state)
        return {
            "fired": True,
            "examined": examined,
            "written": written,
            "multi_stage": bool(multi_stage),
        }
    except Exception as e:
        state["last_error"] = str(e)[:500]
        write_state(state)
        return {"fired": True, "error": str(e), "multi_stage": bool(multi_stage)}


__all__ = [
    "state_path",
    "read_state",
    "write_state",
    "should_run_now",
    "tick",
]
