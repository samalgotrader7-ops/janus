"""
triggers/runtime.py — Phase 6 — daemon loop + fire dispatcher.

The daemon polls every DAEMON_POLL_SECONDS:
  - cron: are we within the matching minute?
  - file_change: have any matching files changed since last check?
  - log_pattern: do any new log.jsonl lines match?
  - interval: have N seconds elapsed since last fire?

A fired trigger:
  1. Honors dedupe_seconds.
  2. Loads the named skill (REQUIRED — no skill = no fire).
  3. Runs interpreter+executor with the trigger's `request` string,
     capability-bound to the skill's tokens.
  4. Routes the resulting output to the configured gateway:
       - "log" (default): writes a record to log.jsonl
       - "telegram": pushes the output to JANUS_TELEGRAM_CHATS
"""

from __future__ import annotations
import datetime as dt
import time
from typing import Callable

from .. import config, interpreter, executor, logger, memory, skills as skills_mod
from ..tools import default_registry, make_capability_aware, CapabilitySet
from .base import (
    Trigger, FireEvent, list_triggers, read_state, write_state,
    cron_due, file_glob_changed, log_pattern_match, interval_due,
)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _now_minute_tuple() -> tuple[int, int, int, int, int]:
    n = dt.datetime.now()
    return (n.minute, n.hour, n.day, n.month, n.weekday())


def _within_dedupe(t: Trigger, last_fired_iso: str | None) -> bool:
    if not t.dedupe_seconds or not last_fired_iso:
        return False
    try:
        last = dt.datetime.fromisoformat(last_fired_iso)
        return (dt.datetime.now(dt.timezone.utc) - last).total_seconds() < t.dedupe_seconds
    except ValueError:
        return False


def _check_trigger(t: Trigger, state: dict, log_tail: list[str]) -> tuple[bool, dict]:
    """Decide if `t` should fire now. Returns (fires, detail)."""
    last = state.get(t.name)
    if _within_dedupe(t, last):
        return False, {"reason": "dedupe"}
    if t.kind == "cron":
        if cron_due(t.when, _now_minute_tuple()):
            return True, {}
    elif t.kind == "interval":
        if interval_due(t.when, last, _now_iso()):
            return True, {}
    elif t.kind == "file_change":
        last_check = state.get(f"{t.name}::file_mtime", 0)
        try:
            last_check = float(last_check)
        except (TypeError, ValueError):
            last_check = 0
        fired, files = file_glob_changed(t.when, last_check)
        if fired:
            return True, {"files": files}
    elif t.kind == "log_pattern":
        hits = log_pattern_match(t.when, log_tail)
        if hits:
            return True, {"matches": hits[:5]}
    return False, {}


# ---------- Notifier ----------


def _notify_log(event: FireEvent, output: str) -> None:
    logger.write({
        "ts": _now_iso(),
        "type": "trigger_fire",
        "trigger": event.trigger,
        "request": event.request,
        "output": output,
        "detail": event.detail,
    })


def _notify_telegram(event: FireEvent, output: str) -> None:
    """Fire-and-forget HTTP POST. Tiny — no python-telegram-bot dep here."""
    import json as _json
    import urllib.request
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_ALLOWED_CHATS:
        _notify_log(event, output)
        return
    chat_ids = [c.strip() for c in config.TELEGRAM_ALLOWED_CHATS.split(",") if c.strip()]
    body = f"🔔 *{event.trigger}*\n\n{output[:3500]}"
    for cid in chat_ids:
        try:
            payload = _json.dumps({
                "chat_id": cid, "text": body, "parse_mode": "Markdown",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                data=payload, headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            continue
    _notify_log(event, output)


def _make_notifier() -> Callable[[FireEvent, str], None]:
    if config.DAEMON_NOTIFY_GATEWAY == "telegram":
        return _notify_telegram
    return _notify_log


# ---------- Auto-approver for triggers ----------


def _auto_approver(action_label: str, details: str, **kw) -> bool:
    """Triggers run unattended. Capability tokens are the SOLE gate."""
    return False  # base default = deny; capability wrapper short-circuits to True


# ---------- Single-fire ----------


def fire_once(t: Trigger, *, detail: dict | None = None) -> str:
    """Run a single trigger now. Used by tests + manual `python -m janus fire <name>`."""
    if not t.skill:
        return f"trigger {t.name}: no skill specified — refusing to fire"
    skill = skills_mod.load(t.skill)
    if skill is None:
        return f"trigger {t.name}: skill {t.skill!r} not found"

    interps = interpreter.interpret(
        t.request,
        memory_preamble=memory.prepend_for_prompt(),
        skill_hints=f"- {skill.name} ({skill.state}): {skill.description}",
        temperature=0.3,
    )
    chosen = interps[0] if interps else {
        "label": "trigger-fallback", "action": t.request, "risk": "—"
    }

    caps = skill.capabilities
    tools = default_registry(capabilities=caps)
    approver = make_capability_aware(_auto_approver, caps)
    output, trace = executor.execute(
        original_request=t.request,
        chosen_label=chosen["label"],
        chosen_action=chosen["action"],
        tools=tools, approver=approver,
        on_step=None,
        skill_body=skill.body,
        memory_preamble=memory.prepend_for_prompt(),
    )
    event = FireEvent(
        trigger=t.name,
        request=t.request,
        skill=t.skill,
        fired_at=_now_iso(),
        detail=detail or {},
    )
    _make_notifier()(event, output)
    return output


# ---------- Daemon loop ----------


def _read_log_tail(n: int = 20) -> list[str]:
    if not config.LOG_FILE.exists():
        return []
    try:
        with config.LOG_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 50_000))
            chunk = f.read().decode("utf-8", errors="ignore")
        return chunk.splitlines()[-n:]
    except OSError:
        return []


def run_daemon(*, once: bool = False) -> None:
    """Polling loop. Set once=True for a single iteration (tests)."""
    config.assert_configured()
    config.ensure_home()
    print(f"janus daemon: polling every {config.DAEMON_POLL_SECONDS}s "
          f"({len(list_triggers())} triggers, gateway={config.DAEMON_NOTIFY_GATEWAY})")

    while True:
        triggers = [t for t in list_triggers() if t.enabled]
        state = read_state()
        log_tail = _read_log_tail()
        for t in triggers:
            try:
                fires, detail = _check_trigger(t, state, log_tail)
            except Exception as e:
                print(f"  [!] {t.name} check failed: {e}")
                continue
            if not fires:
                continue
            print(f"  [+] firing: {t.name} ({t.kind})")
            try:
                output = fire_once(t, detail=detail)
                state[t.name] = _now_iso()
                if t.kind == "file_change":
                    state[f"{t.name}::file_mtime"] = str(time.time())
                write_state(state)
                head = output.splitlines()[:1]
                print(f"      → {head[0][:80] if head else '(no output)'}")
            except Exception as e:
                print(f"      [!] fire failed: {e}")
        if once:
            return
        time.sleep(config.DAEMON_POLL_SECONDS)
