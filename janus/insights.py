"""
insights.py — periodic activity summary (v1.9.0, Tier A item 3).

WHY THIS EXISTS:
Without insights, Janus's accumulated state is opaque. The user knows
they've been using it but has no view of HOW: which models cost the
most, which tools fire most often, which agents are reliable, what the
memory has been growing toward. Hermes calls this `agent/insights.py`;
v1.9 ports the pattern.

DESIGN — DETERMINISTIC FIRST:
Hermes' insights uses an LLM pass to narrate the numbers. v1.9 ships
the deterministic core only — counts, top-N tables, freshness checks.
Always works (no API key needed for `janus insights`), fast, and
predictable. An LLM "narrate this" pass can come later as an optional
enhancement; the structured stats remain the source of truth.

WINDOW:
Default 7 days. Caller can pass any int. Reads:
  - ~/.janus/log.jsonl (audit trail of every interaction + tool call)
  - ~/.janus/cost.jsonl (per-turn cost ledger)
  - ~/.janus/daemon.state.json (last-fired times per trigger)
  - ~/.janus/cron/output/ (archive of cron fires)
  - ~/.janus/conversations/ (sessions)

OUTPUT:
A dict with structured sections + a render_insights() helper that
produces markdown for `/insights` slash + `janus insights` CLI.
Sections are independent — if log.jsonl doesn't exist, that section
is just "no data" and the others still work.

P5 (plain-text state): all the inputs are files the user can `cat`.
We're aggregating, not magic.
"""

from __future__ import annotations
import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Any

from . import config


# ---------- Top-level entrypoint ----------


def compute_insights(days: int = 7) -> dict[str, Any]:
    """Read all sources, return a structured summary dict.

    Keys:
      window_days: int
      since: ISO timestamp (UTC, days ago)
      activity: {turn_count, tool_call_count, by_tool, by_day}
      cost:     {total_usd, by_model, by_chat}
      agents:   {fire_count, by_agent, recent_fires}
      memory:   {audit_count, recent_diffs, category_sizes}
      sessions: {count, recent_titles}
    """
    days = max(1, int(days))
    since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    since_iso = since_dt.isoformat(timespec="seconds")

    return {
        "window_days": days,
        "since": since_iso,
        "activity": _activity_stats(since_dt),
        "cost": _cost_stats(since_dt),
        "agents": _agent_stats(since_dt),
        "memory": _memory_stats(since_dt),
        "sessions": _session_stats(since_dt),
    }


# ---------- Activity (log.jsonl) ----------


def _activity_stats(since_dt: dt.datetime) -> dict[str, Any]:
    out = {"turn_count": 0, "tool_call_count": 0, "by_tool": {}, "by_day": {}}
    if not config.LOG_FILE.is_file():
        return out
    by_tool: Counter = Counter()
    by_day: Counter = Counter()
    turns = 0
    tool_calls = 0
    try:
        with config.LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("ts", "")
                if not ts or not _ts_in_window(ts, since_dt):
                    continue
                day = ts[:10]
                t = rec.get("type", "")
                if t in ("turn", "interaction", "chat_turn"):
                    turns += 1
                    by_day[day] += 1
                tool_name = rec.get("tool")
                if tool_name:
                    tool_calls += 1
                    by_tool[str(tool_name)] += 1
    except OSError:
        return out
    out["turn_count"] = turns
    out["tool_call_count"] = tool_calls
    out["by_tool"] = dict(by_tool.most_common(10))
    out["by_day"] = dict(sorted(by_day.items()))
    return out


# ---------- Cost ----------


def _cost_stats(since_dt: dt.datetime) -> dict[str, Any]:
    out = {"total_usd": 0.0, "by_model": {}, "by_chat": {}}
    cost_file = config.HOME / "cost.jsonl"
    if not cost_file.is_file():
        return out
    total = 0.0
    by_model: Counter = Counter()
    by_chat: Counter = Counter()
    try:
        with cost_file.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("ts", "")
                if not ts or not _ts_in_window(ts, since_dt):
                    continue
                cost = float(rec.get("cost_usd") or rec.get("usd") or 0.0)
                total += cost
                model = str(rec.get("model") or "")
                chat = str(rec.get("chat_id") or rec.get("chat") or "")
                if model:
                    by_model[model] += cost
                if chat:
                    by_chat[chat] += cost
    except OSError:
        return out
    out["total_usd"] = round(total, 4)
    out["by_model"] = {k: round(v, 4) for k, v in by_model.most_common(5)}
    out["by_chat"] = {k: round(v, 4) for k, v in by_chat.most_common(5)}
    return out


# ---------- Agents (cron fires) ----------


def _agent_stats(since_dt: dt.datetime) -> dict[str, Any]:
    out = {"fire_count": 0, "by_agent": {}, "recent_fires": []}
    archive = config.HOME / "cron" / "output"
    if not archive.is_dir():
        return out
    by_agent: Counter = Counter()
    fires: list[tuple[str, str]] = []  # (timestamp, agent)
    for agent_dir in archive.iterdir():
        if not agent_dir.is_dir():
            continue
        for f in agent_dir.glob("*.md"):
            ts = _unmangle_filename_ts(f.stem)
            try:
                fired = dt.datetime.fromisoformat(ts)
            except ValueError:
                continue
            if fired.tzinfo is None:
                fired = fired.replace(tzinfo=dt.timezone.utc)
            if fired < since_dt:
                continue
            by_agent[agent_dir.name] += 1
            fires.append((ts, agent_dir.name))
    out["fire_count"] = sum(by_agent.values())
    out["by_agent"] = dict(by_agent.most_common(10))
    fires.sort(reverse=True)
    out["recent_fires"] = [
        {"fired_at": ts, "agent": name} for ts, name in fires[:5]
    ]
    return out


# ---------- Memory ----------


def _memory_stats(since_dt: dt.datetime) -> dict[str, Any]:
    out = {"audit_count": 0, "recent_diffs": [], "category_sizes": {}}
    if config.MEMORY_DIR.is_dir():
        sizes: dict[str, int] = {}
        for p in config.MEMORY_DIR.glob("*.md"):
            try:
                sizes[p.stem] = len(p.read_text(encoding="utf-8"))
            except OSError:
                continue
        out["category_sizes"] = dict(
            sorted(sizes.items(), key=lambda kv: -kv[1])
        )

    audit_dir = config.MEMORY_DIR / "_audit"
    if audit_dir.is_dir():
        recent: list[tuple[str, str, str]] = []  # (ts, agent, path)
        for p in audit_dir.glob("*.md"):
            stem = p.stem  # "<ts>__<agent>"
            if "__" not in stem:
                continue
            ts_part, agent = stem.split("__", 1)
            ts = _unmangle_filename_ts(ts_part)
            try:
                t_dt = dt.datetime.fromisoformat(ts)
            except ValueError:
                continue
            if t_dt.tzinfo is None:
                t_dt = t_dt.replace(tzinfo=dt.timezone.utc)
            if t_dt < since_dt:
                continue
            recent.append((ts, agent, str(p)))
        recent.sort(reverse=True)
        out["audit_count"] = len(recent)
        out["recent_diffs"] = [
            {"fired_at": ts, "agent": a, "path": p}
            for ts, a, p in recent[:5]
        ]
    return out


# ---------- Sessions ----------


def _session_stats(since_dt: dt.datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"count": 0, "recent_titles": []}
    if not config.CONVERSATIONS_DIR.is_dir():
        return out
    convos: list[tuple[str, str, str]] = []  # (last_updated, id, title)
    for p in config.CONVERSATIONS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        last = data.get("last_updated", "")
        if not last or not _ts_in_window(last, since_dt):
            continue
        title = data.get("title") or _first_request_preview(data)
        convos.append((last, data.get("id", p.stem), title))
    convos.sort(reverse=True)
    out["count"] = len(convos)
    out["recent_titles"] = [
        {"id": cid, "title": title, "last_updated": last}
        for last, cid, title in convos[:5]
    ]
    return out


def _first_request_preview(data: dict) -> str:
    turns = data.get("turns") or []
    if turns:
        req = (turns[0].get("request") or "").strip()
        return req[:60] + ("…" if len(req) > 60 else "")
    return "(empty)"


# ---------- Helpers ----------


def _unmangle_filename_ts(stem: str) -> str:
    """Reverse `<iso_ts>.replace(":", "-")` from triggers/runtime archive
    filenames. Date keeps its hyphens (YYYY-MM-DD); time + TZ get their
    colons back: `2026-05-04T13-00-00+00-00` → `2026-05-04T13:00:00+00:00`.
    """
    if "T" not in stem:
        return stem
    date_part, _, rest = stem.partition("T")
    return f"{date_part}T{rest.replace('-', ':')}"


def _ts_in_window(ts: str, since_dt: dt.datetime) -> bool:
    """Return True if `ts` (ISO-8601 string) is on/after `since_dt`."""
    if not ts:
        return False
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d >= since_dt


# ---------- Render ----------


def render_insights(stats: dict[str, Any]) -> str:
    """Convert the stats dict into a human-readable markdown report."""
    lines: list[str] = []
    lines.append(
        f"# Insights (last {stats['window_days']} day(s) — since {stats['since']})\n"
    )

    a = stats["activity"]
    lines.append("## Activity")
    if a["turn_count"] == 0 and a["tool_call_count"] == 0:
        lines.append("- (no recorded activity in this window)")
    else:
        lines.append(f"- **{a['turn_count']}** chat turns")
        lines.append(f"- **{a['tool_call_count']}** tool calls")
        if a["by_tool"]:
            top = ", ".join(f"`{t}`×{n}" for t, n in a["by_tool"].items())
            lines.append(f"- top tools: {top}")
        if a["by_day"]:
            days_str = ", ".join(f"{d}: {n}" for d, n in a["by_day"].items())
            lines.append(f"- daily turns: {days_str}")
    lines.append("")

    c = stats["cost"]
    lines.append("## Cost")
    if c["total_usd"] == 0:
        lines.append("- (no recorded cost in this window — likely a free model)")
    else:
        lines.append(f"- **${c['total_usd']}** total")
        if c["by_model"]:
            for m, v in c["by_model"].items():
                lines.append(f"  - `{m}`: ${v}")
        if c["by_chat"]:
            lines.append("- by chat:")
            for chat, v in c["by_chat"].items():
                lines.append(f"  - {chat}: ${v}")
    lines.append("")

    ag = stats["agents"]
    lines.append("## Scheduled agents")
    if ag["fire_count"] == 0:
        lines.append("- (no agent fires in this window)")
    else:
        lines.append(f"- **{ag['fire_count']}** fires")
        for name, n in ag["by_agent"].items():
            lines.append(f"  - {name}: {n}×")
        if ag["recent_fires"]:
            lines.append("- recent fires:")
            for f in ag["recent_fires"]:
                lines.append(f"  - {f['fired_at']} → {f['agent']}")
    lines.append("")

    m = stats["memory"]
    lines.append("## Memory")
    if m["category_sizes"]:
        sizes = ", ".join(
            f"`{k}.md` ({v}B)" for k, v in m["category_sizes"].items()
        )
        lines.append(f"- categories: {sizes}")
    if m["audit_count"]:
        lines.append(f"- **{m['audit_count']}** autonomous diffs (cron-written)")
        for d in m["recent_diffs"]:
            lines.append(f"  - {d['fired_at']} via {d['agent']}")
    if not m["category_sizes"] and not m["audit_count"]:
        lines.append("- (no memory data in this window)")
    lines.append("")

    s = stats["sessions"]
    lines.append("## Sessions")
    if s["count"] == 0:
        lines.append("- (no recent sessions)")
    else:
        lines.append(f"- **{s['count']}** sessions touched")
        for t in s["recent_titles"]:
            lines.append(f"  - {t['last_updated']} · {t['title']} (`{t['id']}`)")

    return "\n".join(lines).rstrip() + "\n"
