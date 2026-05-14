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

from .. import app as janus_app
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
    """Run a single trigger now. Used by tests + manual `python -m janus fire <name>`.

    v1.7.0: switched from legacy executor.execute (interpret-then-execute,
    pre-1.0 architecture) to executor.chat with mode="auto". This means
    fired agents now get:
      - The full JANUS_CHAT_SYSTEM rule set (Rule 10 about agent_create,
        the WRONG/RIGHT examples, etc.)
      - The unattended preamble injected into skill.body by agent_create
      - The auto-mode safety analyzer (blocks rm -rf /, SSRF, etc.)
        instead of a pure auto-approver that allowed everything
      - The same on_step heartbeats and structured trace shape that the
        chat surfaces use
    Net: fired agents and chat agents are now ONE codepath. The behavior
    differences come purely from the skill body's UNATTENDED preamble +
    mode=auto, not from a separate execution path with its own bugs.
    """
    if not t.skill:
        return f"trigger {t.name}: no skill specified — refusing to fire"
    skill = skills_mod.load(t.skill)
    if skill is None:
        return f"trigger {t.name}: skill {t.skill!r} not found"

    caps = skill.capabilities
    # If the skill restricted tools via frontmatter `tool_names`, honor it.
    tool_names = None
    fm = skill.raw_frontmatter or {}
    if isinstance(fm.get("tool_names"), list):
        tool_names = [str(n) for n in fm["tool_names"]]
    tools = default_registry(capabilities=caps, tool_names=tool_names)
    # Auto-mode: tool calls auto-approve but auto_mode.py risk patterns
    # still block dangerous calls (rm -rf /, ~/.ssh writes, SSRF).
    # Capability tokens additionally short-circuit to ALLOW.
    from ..tools import make_protected
    approver = make_protected(_auto_approver, caps, "auto")

    # v1.25.7 Phase 0e: route through the substrate so scheduled
    # agents see the same event-stream features (hook_fired,
    # memory_recall, etc.) as interactive surfaces.
    output, _trace = janus_app.run_turn(
        messages=[],
        user_input=t.request,
        tools=tools,
        approver=approver,
        on_step=None,
        skill_body=skill.body,
        memory_preamble=memory.prepend_for_prompt(),
        mode="auto",
        workspace=str(config.WORKSPACE),
        tool_count=len(tools.schemas()),
        skill_count=1,
        stream=False,  # no console to stream to in unattended mode
    )

    # v1.7.0 — opt-in: skill frontmatter `memory-write: true` lets the
    # fired agent propose memory diffs against its own output. Diffs are
    # auto-applied (no human to approve) AND mirrored to
    # ~/.janus/memory/_audit/ so the user can review what the agent
    # added on its own initiative.
    if fm.get("memory-write") or fm.get("memory_write"):
        try:
            _propose_and_audit_diff(t, skill, output)
        except Exception as e:
            logger.write({
                "ts": _now_iso(),
                "type": "memory_write_failed",
                "trigger": t.name,
                "error": f"{type(e).__name__}: {e}",
            })

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


# ---------- Memory write from cron (v1.7.0 opt-in) ----------


def _propose_and_audit_diff(t: Trigger, skill, output: str) -> None:
    """Propose memory ops from a fired agent's output, auto-apply, audit.

    Triggered when the skill's frontmatter has `memory-write: true`.
    The fired agent runs unattended — there's no human to approve a diff
    interactively — so we apply the diff AND write a copy to
    ~/.janus/memory/_audit/<ts>__<agent>.md so the user can review and
    revert via `/memory` slash commands.

    Conservative scope: pre-v1.8 we only allow ops to project.md and
    relationships.md (the two categories that legitimately accumulate
    over time from autonomous activity). Other categories (soul, user,
    preferences) are too identity-shaped to be touched without a human.
    """
    from .. import memory as memory_mod  # local: avoid circular import
    result = memory_mod.propose_diff(t.request, output)
    ops = result.get("ops") or []
    cards = result.get("cards") or []
    # Filter ops to allowed categories.
    allowed = {"project", "relationships"}
    ops = [op for op in ops if (op.get("category") or "user") in allowed]
    # v1.18: cards from autonomous fires also auto-apply (no UI to confirm)
    # but stay scoped to current origin (daemon — not global) per the
    # session_context defaults.
    if not ops and not cards:
        return

    # Apply to live memory.
    if ops:
        memory_mod.apply(ops)
    if cards:
        memory_mod.apply_cards(cards, gateway="daemon")

    # Mirror to audit log.
    audit_dir = config.MEMORY_DIR / "_audit"
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        ts_safe = _now_iso().replace(":", "-")
        audit_path = audit_dir / f"{ts_safe}__{t.name}.md"
        audit_path.write_text(
            f"# memory diff applied by agent fire\n\n"
            f"trigger: {t.name}\n"
            f"skill: {skill.name}\n"
            f"fired_at: {_now_iso()}\n"
            f"request: {t.request!r}\n\n"
            f"## proposed ops\n\n"
            f"{memory_mod.render_diff(ops)}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


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

    # v1.30.2 — built-in memory consolidation cron (opt-in via env).
    consolidate_hours = config.MEMORY_CONSOLIDATE_HOURS
    consolidate_label = (
        f", consolidate every {consolidate_hours}h"
        if consolidate_hours > 0 else ""
    )
    print(f"janus daemon: polling every {config.DAEMON_POLL_SECONDS}s "
          f"({len(list_triggers())} triggers, "
          f"gateway={config.DAEMON_NOTIFY_GATEWAY}"
          f"{consolidate_label})")

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
        # v1.30.2 — memory consolidation tick. No-op when disabled or
        # not due. Failures are swallowed inside tick() so a flaky
        # consolidator can't break the trigger daemon.
        try:
            from .. import memory_consolidate_cron
            ms = memory_consolidate_cron.tick(
                on_fire=lambda multi: print(
                    f"  [+] memory consolidate "
                    f"({'multi-stage' if multi else 'single'})"
                ),
            )
            if ms.get("fired"):
                if ms.get("error"):
                    print(f"      [!] consolidate failed: {ms['error']}")
                else:
                    print(
                        f"      → examined={ms.get('examined', 0)} "
                        f"written={ms.get('written', 0)}"
                    )
        except Exception as e:
            print(f"  [!] memory consolidate tick failed: {e}")
        # v1.43.0 — pruning ticks. Pure-compute, default-on at 24h.
        try:
            from .. import prune_cron
            mp = prune_cron.tick_memory(
                on_fire=lambda: print("  [+] memory prune"),
            )
            if mp.get("fired"):
                if mp.get("error"):
                    print(f"      [!] memory prune failed: {mp['error']}")
                else:
                    print(f"      → removed={mp.get('removed', 0)}")
            sp = prune_cron.tick_skill(
                on_fire=lambda: print("  [+] skill prune"),
            )
            if sp.get("fired"):
                if sp.get("error"):
                    print(f"      [!] skill prune failed: {sp['error']}")
                else:
                    print(
                        f"      → trashed={sp.get('trashed', 0)} "
                        f"stale_marked={sp.get('stale_marked', 0)} "
                        f"unlinked={sp.get('unlinked', 0)}"
                    )
        except Exception as e:
            print(f"  [!] prune tick failed: {e}")
        if once:
            return
        time.sleep(config.DAEMON_POLL_SECONDS)
