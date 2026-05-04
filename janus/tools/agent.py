"""
tools/agent.py — model-callable AGENT lifecycle tools (v1.6.0).

THE LIFETIME-SOLUTION TOOL SET. Before v1.6, when a user said "build me an
AI agent named Samoul that fetches AI news every 4 hours and sends to my
Telegram", the model had no actual machinery — so it'd update memory
(claiming the agent existed) and then fail when asked to "run it now".
That was the lying-chatbot pattern.

These five tools end that pattern by making the agent ABSTRACTION real:

  An "agent" = one skill (~/.janus/skills/<name>.md, the brain) +
               one trigger (~/.janus/triggers/<name>.yaml, the schedule).

The triggers/runtime.py daemon (already shipped in Phase 6) polls the
trigger files and fires fire_once() — which loads the skill, runs the
chat loop, and routes output to the configured `deliver_to` channel.
v1.6 wired per-trigger `deliver_to` so each agent can target its own
chat (telegram:CHAT_ID) instead of the global JANUS_TELEGRAM_CHATS env.

THE TOOLS:

  agent_create(name, purpose, schedule, deliver_to, tool_names?,
               capabilities?, system_prompt?)
    Atomically writes the skill + trigger files. Refuses if either
    already exists (use agent_delete first to overwrite).

  agent_list()
    Returns all agents with: enabled? · schedule · last_fired ·
    deliver_to · linked skill state.

  agent_run_now(name)
    Synchronously fires the agent once via triggers.fire_once. Used
    when the user says "run it now" and doesn't want to wait for the
    next cron tick.

  agent_delete(name)
    Removes both the skill and the trigger.

  agent_set_enabled(name, enabled)
    Toggles trigger.enabled — the agent stays installed but doesn't
    fire on its schedule. Useful for pausing without losing state.

SCHEDULE PARSING:
  Accepts model-friendly natural strings:
    "every 4 hours"   → kind=interval when="14400"
    "every 30 min"    → kind=interval when="1800"
    "every morning at 7am" → kind=cron when="0 7 * * *"
    "every monday at 9am"  → kind=cron when="0 9 * * 1"
  AND raw cron / interval forms:
    "cron:0 */4 * * *"     → kind=cron when="0 */4 * * *"
    "interval:14400"       → kind=interval when="14400"
  Returns ValueError with a clear message if it can't parse.

DAEMON STATUS:
  agent_create checks for ~/.janus/daemon.pid. If absent, the success
  message INCLUDES a hint that the user must run `janus daemon` (or
  systemd unit, etc.) for the agent to actually fire on its schedule.
  We don't auto-spawn the daemon — a backgrounded subprocess with no
  stdout/stderr destination is a footgun, and the user's deployment
  shape (systemd vs tmux vs screen vs nohup) is theirs to choose.

P-INVARIANTS:
  P5 plain-text: skills are markdown, triggers are YAML — `cat`-able.
  P8 errors: every failure returns an observation string the model reads.
  P9 risk: all five tools are risk="write" — they modify ~/.janus/ state.
"""

from __future__ import annotations
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from .base import Tool


# ---------- Schedule parsing ----------


_INTERVAL_UNITS = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


_TIME_OF_DAY_RX = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)


_DAY_OF_WEEK = {
    "sunday": 0, "sun": 0,
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2, "tues": 2,
    "wednesday": 3, "wed": 3,
    "thursday": 4, "thu": 4, "thurs": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}


@dataclass
class ParsedSchedule:
    kind: str   # "cron" | "interval"
    when: str


def parse_schedule(spec: str) -> ParsedSchedule:
    """Convert a model/human schedule string to (kind, when).

    Supported forms:
      "cron:<expr>"          → cron with the literal 5-field expression
      "interval:<seconds>"   → interval with the literal seconds count
      "every <N> <unit>"     → interval (units: s/m/h/d)
      "every morning at 7"   → cron 0 7 * * *
      "every <weekday> at 9am" → cron 0 9 * * <dow>
      "daily at 7am"         → cron 0 7 * * *
      "hourly"               → interval 3600
    """
    s = (spec or "").strip().lower()
    if not s:
        raise ValueError("empty schedule")

    # Explicit prefix forms — pass through untouched (after stripping).
    if s.startswith("cron:"):
        expr = s[5:].strip()
        if len(expr.split()) != 5:
            raise ValueError(
                f"cron expression must have 5 fields, got {expr!r}"
            )
        return ParsedSchedule("cron", expr)
    if s.startswith("interval:"):
        n = s[9:].strip()
        if not n.isdigit() or int(n) <= 0:
            raise ValueError(
                f"interval must be positive seconds integer, got {n!r}"
            )
        return ParsedSchedule("interval", n)

    # "hourly" / "daily" shortcuts.
    if s == "hourly":
        return ParsedSchedule("interval", "3600")
    if s == "daily":
        return ParsedSchedule("cron", "0 7 * * *")

    # "every N unit" → interval.
    m = re.match(
        r"every\s+(\d+)\s*([a-z]+)\b",
        s,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2).rstrip(".")
        if unit in _INTERVAL_UNITS and n > 0:
            return ParsedSchedule("interval", str(n * _INTERVAL_UNITS[unit]))

    # "every morning [at 7]" / "daily at 7am" → cron.
    if "morning" in s:
        hour, minute = _extract_hour_minute(s, default_hour=7)
        return ParsedSchedule("cron", f"{minute} {hour} * * *")
    if "evening" in s:
        hour, minute = _extract_hour_minute(s, default_hour=18)
        return ParsedSchedule("cron", f"{minute} {hour} * * *")
    if "night" in s:
        hour, minute = _extract_hour_minute(s, default_hour=22)
        return ParsedSchedule("cron", f"{minute} {hour} * * *")

    # "every <weekday> [at H[:MM][am/pm]]"
    for day_name, dow in _DAY_OF_WEEK.items():
        if re.search(rf"\b{day_name}\b", s):
            hour, minute = _extract_hour_minute(s, default_hour=9)
            return ParsedSchedule(
                "cron", f"{minute} {hour} * * {dow}"
            )

    # "at H[:MM][am/pm]" alone → daily at that time.
    if "at " in s or "every day" in s:
        hour, minute = _extract_hour_minute(s, default_hour=7)
        return ParsedSchedule("cron", f"{minute} {hour} * * *")

    raise ValueError(
        f"could not parse schedule {spec!r}. Use forms like "
        f'"every 4 hours", "every morning at 7am", "every monday at 9am", '
        f'or explicit "cron:0 */4 * * *" / "interval:14400".'
    )


def _extract_hour_minute(text: str, *, default_hour: int) -> tuple[int, int]:
    m = _TIME_OF_DAY_RX.search(text)
    if not m:
        return default_hour, 0
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    return hour % 24, minute % 60


# ---------- Helpers ----------


_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def _validate_name(name: str) -> str | None:
    if not name:
        return "name required"
    if not _NAME_RX.match(name):
        return (
            f"invalid name {name!r}: must be lowercase, start with a "
            f"letter/digit, only letters/digits/dashes/underscores, "
            f"max 41 chars"
        )
    return None


def _skill_path(name: str) -> Path:
    return config.SKILLS_DIR / f"{name}.md"


def _trigger_path(name: str) -> Path:
    return config.TRIGGERS_DIR / f"{name}.yaml"


def _daemon_running_hint() -> str:
    """One-line hint about whether the trigger daemon is up."""
    pid_file = config.HOME / "daemon.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            return "daemon.pid present but unreadable — restart the daemon"
        # Best-effort liveness check (cross-platform).
        try:
            import os as _os
            _os.kill(pid, 0)  # POSIX: signal 0 = liveness probe
            return f"daemon running (pid {pid})"
        except OSError:
            return "daemon.pid stale — daemon not running. Start: `janus daemon`"
        except AttributeError:
            return f"daemon.pid present (pid {pid}) — liveness check unsupported"
    return (
        "daemon NOT running — agent will be installed but won't fire on "
        "schedule until you run: `janus daemon` (or set up systemd / cron / "
        "tmux session). Use `agent_run_now` to test it once now."
    )


def _yaml_str(v: Any) -> str:
    """Quote a value for our hand-rolled YAML writer."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if not s:
        return '""'
    if any(ch in s for ch in (":", "#", "\n", "'", '"', "{", "}", "[", "]")) or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _build_skill_md(
    *,
    name: str,
    description: str,
    purpose: str,
    capabilities: dict | None,
    tool_names: list[str] | None,
    system_prompt: str,
) -> str:
    """Render a skill markdown file.

    The skill body becomes the system prompt the agent runs with when
    its trigger fires.
    """
    fm: list[str] = ["---"]
    fm.append(f"name: {name}")
    fm.append(f"description: {_yaml_str(description)}")
    fm.append("state: trusted-supervised")
    if tool_names:
        fm.append("tool_names:")
        for n in tool_names:
            fm.append(f"  - {n}")
    if capabilities:
        fm.append("capabilities:")
        for verb_key, targets in capabilities.items():
            fm.append(f"  {verb_key}:")
            tgts = targets if isinstance(targets, list) else [targets]
            for t in tgts:
                fm.append(f"    - {_yaml_str(t)}")
    else:
        fm.append("capabilities: {}")
    fm.append(f"created: {_iso_now()}")
    fm.append("last-promoted: null")
    fm.append("runs: 0")
    fm.append("success: 0")
    fm.append("fail: 0")
    fm.append("---")
    if system_prompt:
        # Custom prompt provided — still prepend the unattended preamble
        # so the agent doesn't ask for confirmation. Without this, custom
        # prompts inherit the chat-mode "ask the user" reflex from
        # JANUS_CHAT_SYSTEM and break when fired (the J31 bug Sam hit).
        body = _UNATTENDED_PREAMBLE + system_prompt.strip()
    else:
        body = _default_body(name, purpose)
    return "\n".join(fm) + "\n\n" + body + "\n"


_UNATTENDED_PREAMBLE = (
    "# YOU RUN UNATTENDED\n\n"
    "You are a scheduled agent — you fire on a timer, NOT in response to "
    "a user message. NO HUMAN IS WATCHING. The user is not online. They "
    "set you up days/weeks ago and walked away.\n\n"
    "RULES:\n\n"
    "1. **Never ask for confirmation.** Do NOT say \"Should I proceed?\" "
    "/ \"Please confirm\" / \"Would you like me to…\". There's nobody to "
    "answer — your turn ends and the question is lost. Just DO the work.\n\n"
    "2. **Never ask clarifying questions.** Make reasonable assumptions "
    "based on your purpose below. If a fact is genuinely ambiguous, pick "
    "the most defensible option and note the assumption in your output.\n\n"
    "3. **Your final reply IS the delivery.** Whatever text you produce "
    "in your last assistant turn will be sent to the user's configured "
    "channel (Telegram chat, log, etc.). Make it self-contained: "
    "headline, summary, key points, sources/links. The user won't see "
    "your tool-call trace — only your final text.\n\n"
    "4. **Be terse but complete.** This isn't a chat — it's a report. "
    "No greetings, no \"here's what I found:\", no \"let me know if you'd "
    "like more detail\". Get straight to the content.\n\n"
    "5. **Time-box yourself.** Make at most 8 tool calls total. Stop, "
    "synthesize, deliver. If you can't finish cleanly, deliver a partial "
    "report with a note about what was incomplete — do NOT loop trying "
    "to be perfect.\n\n"
    "---\n\n"
)


def _default_body(name: str, purpose: str) -> str:
    return (
        f"{_UNATTENDED_PREAMBLE}"
        f"You are {name}, a scheduled Janus agent.\n\n"
        f"# Your job (every time you fire)\n\n"
        f"{purpose.strip()}\n\n"
        f"# How to do it\n\n"
        f"1. Use the tools you have to gather the facts you need.\n"
        f"2. Synthesize a focused report — the FORMAT depends on your job, "
        f"but keep it scannable: headers / bullet points / short paragraphs.\n"
        f"3. End your report with sources or links if you fetched anything.\n"
        f"4. That report IS your final assistant message — the framework "
        f"sends it to the user's configured channel. Don't call a send tool "
        f"yourself for the main delivery.\n"
    )


def _build_trigger_yaml(
    *,
    name: str,
    description: str,
    schedule: ParsedSchedule,
    request: str,
    deliver_to: str,
    dedupe_seconds: int,
) -> str:
    lines: list[str] = []
    lines.append(f"name: {name}")
    lines.append(f"description: {_yaml_str(description)}")
    lines.append(f"kind: {schedule.kind}")
    lines.append(f"when: {_yaml_str(schedule.when)}")
    lines.append(f"skill: {name}")
    lines.append(f"request: {_yaml_str(request)}")
    lines.append(f"deliver_to: {_yaml_str(deliver_to)}")
    lines.append(f"dedupe_seconds: {dedupe_seconds}")
    lines.append("enabled: true")
    return "\n".join(lines) + "\n"


def _iso_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _validate_deliver_to(s: str) -> str | None:
    """deliver_to format: 'log' | 'telegram:<chat_id>'."""
    if s == "log":
        return None
    if s.startswith("telegram:"):
        cid = s[9:].strip()
        if not cid:
            return "telegram deliver_to needs a chat_id (telegram:123456789)"
        # numeric or starts-with-@ both ok
        if not (cid.lstrip("-").isdigit() or cid.startswith("@")):
            return f"chat_id {cid!r} should be numeric or @channelname"
        return None
    return (
        f"unknown deliver_to {s!r}. Use 'log' or 'telegram:<chat_id>'."
    )


# ---------- agent_create ----------


class AgentCreate(Tool):
    """Create a scheduled, autonomous Janus agent.

    THIS IS THE TOOL TO USE when the user asks you to "build / create /
    schedule an agent that runs every X and sends to Y." Do NOT just
    write to memory and claim an agent exists — without this tool there
    is NO agent.
    """

    name = "agent_create"
    description = (
        "Create a scheduled autonomous agent (skill + trigger pair). "
        "USE THIS TOOL when the user asks to BUILD or SCHEDULE an agent "
        "that runs periodically (e.g., 'every 4 hours', 'every morning'). "
        "Without calling this tool, no agent exists — do not fake it via "
        "memory updates. Returns a status string with the daemon hint."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Lowercase identifier (letters/digits/_/-). Used as "
                    "the filename for both the skill and the trigger."
                ),
            },
            "purpose": {
                "type": "string",
                "description": (
                    "One paragraph describing what this agent should do "
                    "every time it fires. This becomes the agent's job "
                    "description in its own system prompt."
                ),
            },
            "schedule": {
                "type": "string",
                "description": (
                    "When the agent should fire. Natural forms: "
                    "'every 4 hours', 'every 30 min', 'every morning "
                    "at 7am', 'every monday at 9am', 'daily', 'hourly'. "
                    "Or explicit: 'cron:0 */4 * * *', 'interval:14400'."
                ),
            },
            "deliver_to": {
                "type": "string",
                "description": (
                    "Where to send the agent's output each time it fires. "
                    "'log' = append to ~/.janus/log.jsonl only. "
                    "'telegram:<chat_id>' = send to a Telegram chat "
                    "(e.g., 'telegram:123456789'). Look up chat_id via "
                    "session_recent if the user is on Telegram."
                ),
            },
            "tool_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Subset of bundled tool names the agent may use "
                    "(e.g., ['web_search', 'web_fetch', 'fs_read']). "
                    "Empty / omitted = ALL tools available. Restrict for "
                    "security."
                ),
            },
            "capabilities": {
                "type": "object",
                "description": (
                    "Capability tokens to auto-approve dangerous actions "
                    "(maps verb to allowed target patterns). Example: "
                    '{"web.fetch": ["news.google.com/*"]}. '
                    "Without these, the agent will be blocked at every "
                    "dangerous call (since it runs unattended). Add what "
                    "the agent legitimately needs."
                ),
            },
            "system_prompt": {
                "type": "string",
                "description": (
                    "OPTIONAL custom system prompt body. If omitted, a "
                    "default body is generated from `purpose`. Use this "
                    "when you want fine-grained control over the agent's "
                    "behavior (tone, format, multi-step reasoning rules)."
                ),
            },
            "dedupe_seconds": {
                "type": "integer",
                "description": (
                    "Minimum seconds between fires (defense against "
                    "schedule overlap). Default: 0 (no dedup). For "
                    "interval-based agents, leave at 0 — the interval "
                    "itself is the dedup window."
                ),
            },
            "request": {
                "type": "string",
                "description": (
                    "OPTIONAL one-line user-prompt the trigger sends to "
                    "the agent each fire. Defaults to `purpose`. Useful "
                    "if the prompt phrasing differs from the description."
                ),
            },
        },
        "required": ["name", "purpose", "schedule", "deliver_to"],
    }
    risk = "write"

    def run(self, args: dict, approver) -> str:
        config.ensure_home()
        name = (args.get("name") or "").strip().lower()
        err = _validate_name(name)
        if err:
            return f"error: {err}"

        purpose = (args.get("purpose") or "").strip()
        if not purpose:
            return "error: purpose required (one paragraph describing the agent's job)"

        schedule_spec = (args.get("schedule") or "").strip()
        try:
            sched = parse_schedule(schedule_spec)
        except ValueError as e:
            return f"error: {e}"

        deliver_to = (args.get("deliver_to") or "log").strip()
        d_err = _validate_deliver_to(deliver_to)
        if d_err:
            return f"error: {d_err}"

        skill_p = _skill_path(name)
        trig_p = _trigger_path(name)
        if skill_p.exists() or trig_p.exists():
            return (
                f"error: agent {name!r} already exists "
                f"({'skill' if skill_p.exists() else 'trigger'} file present). "
                f"Use agent_delete first to overwrite."
            )

        if not approver(
            f"agent_create → {name}",
            f"schedule={schedule_spec} deliver_to={deliver_to}",
            capability=("agent", "create", name),
        ):
            return f"refused: agent_create({name})"

        tool_names = args.get("tool_names") or None
        if tool_names is not None and not isinstance(tool_names, list):
            return "error: tool_names must be a list of strings"

        capabilities = args.get("capabilities") or None
        if capabilities is not None and not isinstance(capabilities, dict):
            return "error: capabilities must be an object (verb → target list)"

        system_prompt = args.get("system_prompt", "")
        request = (args.get("request") or purpose).strip()
        try:
            dedupe_seconds = int(args.get("dedupe_seconds") or 0)
        except (TypeError, ValueError):
            return "error: dedupe_seconds must be an integer"

        skill_md = _build_skill_md(
            name=name,
            description=purpose[:200],
            purpose=purpose,
            capabilities=capabilities,
            tool_names=tool_names,
            system_prompt=system_prompt,
        )
        trig_yaml = _build_trigger_yaml(
            name=name,
            description=purpose[:200],
            schedule=sched,
            request=request,
            deliver_to=deliver_to,
            dedupe_seconds=dedupe_seconds,
        )

        # Write atomically: temp file then rename. If the trigger write
        # fails, roll the skill back so we don't leave half-installed
        # state.
        try:
            _atomic_write(skill_p, skill_md)
        except OSError as e:
            return f"error: failed to write skill file: {e}"
        try:
            _atomic_write(trig_p, trig_yaml)
        except OSError as e:
            try:
                skill_p.unlink()
            except OSError:
                pass
            return f"error: failed to write trigger file: {e}"

        sched_h = _human_schedule(sched)
        hint = _daemon_running_hint()
        return (
            f"created agent {name!r}\n"
            f"  schedule: {sched_h}\n"
            f"  delivers to: {deliver_to}\n"
            f"  skill: {skill_p}\n"
            f"  trigger: {trig_p}\n"
            f"  daemon: {hint}"
        )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _human_schedule(s: ParsedSchedule) -> str:
    if s.kind == "interval":
        secs = int(s.when)
        if secs % 3600 == 0:
            return f"every {secs // 3600}h (interval:{secs})"
        if secs % 60 == 0:
            return f"every {secs // 60}min (interval:{secs})"
        return f"every {secs}s"
    return f"cron {s.when}"


# ---------- agent_list ----------


class AgentList(Tool):
    """List all installed agents with status."""

    name = "agent_list"
    description = (
        "List all installed scheduled agents. Returns one block per agent "
        "with: enabled status, schedule, delivery target, last fired time. "
        "Use this when the user asks 'what agents do I have' or 'list my "
        "agents' or before deleting one."
    )
    parameters = {"type": "object", "properties": {}}
    risk = "read"

    def run(self, args: dict, approver) -> str:
        from ..triggers.base import list_triggers, read_state

        triggers = list_triggers()
        state = read_state()
        if not triggers:
            return "no agents installed. Use agent_create to make one."

        out: list[str] = []
        for t in triggers:
            # Only show triggers that look like agents (skill exists with
            # same name). Pure-trigger entries from earlier phases are
            # excluded so the listing stays focused.
            skill_p = _skill_path(t.name)
            if not skill_p.exists():
                continue
            last_fired = state.get(t.name, "never")
            deliver_to = t.raw.get("deliver_to", "log") if t.raw else "log"
            sched_h = _human_from_kind_when(t.kind, t.when)
            status = "enabled" if t.enabled else "PAUSED"
            out.append(
                f"- {t.name}  [{status}]\n"
                f"    schedule: {sched_h}\n"
                f"    delivers: {deliver_to}\n"
                f"    last:     {last_fired}\n"
                f"    request:  {(t.request or '')[:80]}"
            )
        if not out:
            return (
                "no agents installed (found triggers but no matching skill files). "
                "Use agent_create to make one."
            )
        return "\n\n".join(out)


def _human_from_kind_when(kind: str, when: str) -> str:
    if kind == "interval":
        try:
            secs = int(when)
        except ValueError:
            return f"interval:{when}"
        if secs % 3600 == 0:
            return f"every {secs // 3600}h"
        if secs % 60 == 0:
            return f"every {secs // 60}min"
        return f"every {secs}s"
    return f"{kind}: {when}"


# ---------- agent_run_now ----------


class AgentRunNow(Tool):
    """Fire an agent once, immediately, regardless of its schedule."""

    name = "agent_run_now"
    description = (
        "Fire an installed agent ONCE right now. Use this when the user "
        "says 'run it now' / 'test the agent' / 'fire X' — don't make "
        "them wait for the next scheduled tick. Synchronous: blocks "
        "until the agent finishes, returns its output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the installed agent (matches the file stem).",
            },
        },
        "required": ["name"],
    }
    risk = "exec"

    def run(self, args: dict, approver) -> str:
        from ..triggers.base import load_triggers
        from ..triggers.runtime import fire_once

        name = (args.get("name") or "").strip().lower()
        err = _validate_name(name)
        if err:
            return f"error: {err}"

        triggers = load_triggers()
        if name not in triggers:
            available = ", ".join(sorted(triggers.keys())) or "(none)"
            return f"error: no agent named {name!r}. Installed: {available}"
        if not _skill_path(name).exists():
            return (
                f"error: trigger {name!r} exists but no matching skill file "
                f"({_skill_path(name)}). Was this installed via agent_create?"
            )

        if not approver(
            f"agent_run_now → {name}",
            f"firing {name} now",
            capability=("agent", "run", name),
        ):
            return f"refused: agent_run_now({name})"

        try:
            output = fire_once(triggers[name])
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"
        # Trim large outputs so the model doesn't blow context.
        if len(output) > 4000:
            output = output[:4000] + f"\n… [+{len(output) - 4000} more chars]"
        return f"fired {name!r} → output:\n\n{output}"


# ---------- agent_delete ----------


class AgentDelete(Tool):
    """Remove both the skill and the trigger for an agent."""

    name = "agent_delete"
    description = (
        "Permanently remove an agent — deletes BOTH the skill and the "
        "trigger file. Confirm with the user before calling unless they "
        "already explicitly asked to delete."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the agent to remove.",
            },
        },
        "required": ["name"],
    }
    risk = "write"

    def run(self, args: dict, approver) -> str:
        name = (args.get("name") or "").strip().lower()
        err = _validate_name(name)
        if err:
            return f"error: {err}"

        skill_p = _skill_path(name)
        trig_p = _trigger_path(name)
        if not skill_p.exists() and not trig_p.exists():
            return f"error: no agent named {name!r} (no skill or trigger file)"

        if not approver(
            f"agent_delete → {name}",
            f"removing {skill_p.name} and {trig_p.name}",
            capability=("agent", "delete", name),
        ):
            return f"refused: agent_delete({name})"

        removed: list[str] = []
        for p in (skill_p, trig_p):
            if p.exists():
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError as e:
                    return f"error: failed to remove {p}: {e}"
        return f"deleted agent {name!r} (removed: {', '.join(removed)})"


# ---------- agent_set_enabled ----------


class AgentSetEnabled(Tool):
    """Pause or resume an agent without deleting it."""

    name = "agent_set_enabled"
    description = (
        "Enable or disable an installed agent's schedule. Disabled agents "
        "stay installed but the daemon skips them. Use to PAUSE an agent "
        "temporarily without losing its config."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the agent.",
            },
            "enabled": {
                "type": "boolean",
                "description": "true = active on schedule. false = paused.",
            },
        },
        "required": ["name", "enabled"],
    }
    risk = "write"

    def run(self, args: dict, approver) -> str:
        name = (args.get("name") or "").strip().lower()
        err = _validate_name(name)
        if err:
            return f"error: {err}"
        enabled = bool(args.get("enabled"))

        trig_p = _trigger_path(name)
        if not trig_p.exists():
            return f"error: no agent named {name!r}"

        if not approver(
            f"agent_set_enabled → {name}",
            f"set enabled={enabled}",
            capability=("agent", "set_enabled", name),
        ):
            return f"refused: agent_set_enabled({name})"

        # Tweak just the `enabled:` line in-place. Hand-rolled YAML so we
        # do a hand-rolled edit too — preserves the user's formatting.
        text = trig_p.read_text(encoding="utf-8")
        new_lines: list[str] = []
        replaced = False
        for line in text.splitlines():
            if line.lstrip().startswith("enabled:"):
                indent = line[: len(line) - len(line.lstrip())]
                new_lines.append(f"{indent}enabled: {'true' if enabled else 'false'}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"enabled: {'true' if enabled else 'false'}")
        _atomic_write(trig_p, "\n".join(new_lines) + "\n")
        return f"agent {name!r} {'enabled' if enabled else 'paused'}"
