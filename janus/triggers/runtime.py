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


def _send_telegram(chat_ids: list[str], event: FireEvent, output: str) -> None:
    """Fire-and-forget HTTP POST to one or more Telegram chats."""
    import json as _json
    import urllib.request
    if not config.TELEGRAM_BOT_TOKEN or not chat_ids:
        return
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


def _notify_telegram_global(event: FireEvent, output: str) -> None:
    """Legacy path — uses JANUS_TELEGRAM_CHATS env var."""
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ALLOWED_CHATS:
        chat_ids = [
            c.strip() for c in config.TELEGRAM_ALLOWED_CHATS.split(",")
            if c.strip()
        ]
        _send_telegram(chat_ids, event, output)
    _notify_log(event, output)


def _notify_per_trigger(deliver_to: str) -> Callable[[FireEvent, str], None]:
    """v1.6 per-trigger delivery: deliver_to overrides global config.

    Format:
      "log"                 → log only
      "telegram:<chat_id>"  → that one chat (still logs too)
      "telegram:<a>,<b>"    → multiple chats
      ""                    → fall back to global config (back-compat)
    """
    s = (deliver_to or "").strip()
    if not s:
        return _make_notifier_global()
    if s == "log":
        return _notify_log
    if s.startswith("telegram:"):
        cids_raw = s[9:].strip()
        chat_ids = [c.strip() for c in cids_raw.split(",") if c.strip()]
        def _go(event: FireEvent, output: str) -> None:
            _send_telegram(chat_ids, event, output)
            _notify_log(event, output)
        return _go
    # Unknown deliver_to → log + warn via the log itself (don't crash).
    def _unknown(event: FireEvent, output: str) -> None:
        _notify_log(event, f"[unknown deliver_to {s!r}]\n\n{output}")
    return _unknown


def _make_notifier_global() -> Callable[[FireEvent, str], None]:
    if config.DAEMON_NOTIFY_GATEWAY == "telegram":
        return _notify_telegram_global
    return _notify_log


# Back-compat alias — v1.5 callers (and tests) used this name.
_make_notifier = _make_notifier_global


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
    # v1.6.1 — archive each fire to ~/.janus/cron/output/<agent>/<ts>.md
    # so the user can browse history without grepping log.jsonl. Hermes
    # uses the same layout (~/.hermes/cron/output/{job_id}/{ts}.md), and
    # matching it now keeps the door open for an easy migration later.
    _archive_fire_output(event, output)
    # v1.6 — honor per-trigger deliver_to. Falls back to global config
    # when the trigger doesn't set one (back-compat with pre-v1.6 yaml).
    _notify_per_trigger(t.deliver_to)(event, output)
    return output


# ---------- Output archive (Hermes-style) ----------


def _archive_fire_output(event: FireEvent, output: str) -> None:
    """Write each fire's output to ~/.janus/cron/output/<agent>/<ts>.md.

    Best-effort — archive failure does NOT abort the fire (the delivery
    notification still happens). Filename uses the fired_at timestamp
    with colons replaced by hyphens so it's safe on Windows too.
    """
    try:
        out_dir = config.HOME / "cron" / "output" / event.trigger
        out_dir.mkdir(parents=True, exist_ok=True)
        ts_safe = event.fired_at.replace(":", "-")
        path = out_dir / f"{ts_safe}.md"
        # Compose a minimal frontmatter for grep-ability + body.
        body = (
            f"---\n"
            f"trigger: {event.trigger}\n"
            f"skill: {event.skill or ''}\n"
            f"fired_at: {event.fired_at}\n"
            f"request: {event.request!r}\n"
            f"---\n\n"
            f"{output}\n"
        )
        path.write_text(body, encoding="utf-8")
    except OSError:
        # Don't crash the fire just because the disk is full / locked.
        pass


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
    """Polling loop. Set once=True for a single iteration (tests).

    v1.6: writes ~/.janus/daemon.pid on entry so agent_create's
    _daemon_running_hint can detect us. The pid file is removed on
    clean exit; a stale pid (process gone) is detected via os.kill(pid, 0).
    """
    import atexit
    import os as _os
    config.assert_configured()
    config.ensure_home()

    pid_file = config.HOME / "daemon.pid"
    # If a previous daemon left a stale pid, overwrite. If a live one
    # is already running we still continue (the user may want a second
    # one for testing) — but warn.
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            try:
                _os.kill(old_pid, 0)
                print(
                    f"  [!] another daemon may already be running "
                    f"(pid {old_pid} from {pid_file}). Continuing."
                )
            except OSError:
                pass  # stale, fine
        except (ValueError, OSError):
            pass
    try:
        pid_file.write_text(str(_os.getpid()), encoding="utf-8")
    except OSError:
        pass

    def _cleanup_pid() -> None:
        try:
            if pid_file.is_file() and pid_file.read_text().strip() == str(_os.getpid()):
                pid_file.unlink()
        except OSError:
            pass
    atexit.register(_cleanup_pid)

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
