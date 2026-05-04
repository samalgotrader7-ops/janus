"""
memory_state.py — live filesystem introspection injected into the prompt
(v1.7.0 — Tier A item 1, "memory wiring").

WHY THIS EXISTS:
Pre-v1.7 the model couldn't answer "what agents do I have?" without
calling agent_list (or worse — flailing through fs_grep loops, the J22
bug Sam hit). Memory contained user/project facts but said nothing
about Janus's OWN state. The model had to discover its own machinery
every turn.

This module reads the filesystem and produces a short markdown block
that prepend_for_prompt() appends to the memory chunk. Now the system
prompt always includes a current snapshot of:

  - installed agents (skill + trigger pair, with schedule + delivery)
  - installed swarm specs
  - recent fires (from daemon.state.json + cron output archive)
  - skill count by lifecycle state

It's CHEAP — just stat/glob calls. Refreshed every prompt build, so
agents created mid-conversation appear next turn. Bounded output: top
N entries + counts; never blows context.

P5 (plain-text state): everything we read here is a file the user can
also `cat`. We're just making it visible to the model.

P7 (bounded everything): hard caps on what we surface — see _CAPS.
"""

from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
from typing import Any

from . import config


# Hard caps so an overgrown state directory can't blow the prompt.
_CAPS = {
    "agents": 30,         # show up to 30 agents
    "swarms": 20,
    "recent_fires": 10,
    "skill_summary_chars": 80,
}


def state_block() -> str:
    """Return the markdown state-introspection block for the system prompt.

    Empty string means there's nothing notable yet (fresh install) — the
    caller skips the block to keep the system prompt tight.
    """
    parts: list[str] = []

    agents_text = _agents_section()
    if agents_text:
        parts.append(agents_text)

    swarms_text = _swarms_section()
    if swarms_text:
        parts.append(swarms_text)

    skills_text = _skills_summary_section()
    if skills_text:
        parts.append(skills_text)

    fires_text = _recent_fires_section()
    if fires_text:
        parts.append(fires_text)

    if not parts:
        return ""
    header = (
        "# Janus state right now (live — reflects ~/.janus/ filesystem)\n\n"
        "When the user asks what's installed, what runs on what schedule, "
        "or what agents/skills/swarms exist — DO NOT GREP. The answer is "
        "below. Use this and call agent_list / agent_run_now / agent_delete "
        "when you need to act on it."
    )
    return header + "\n\n" + "\n\n".join(parts) + "\n"


# ---------- Sections ----------


def _agents_section() -> str:
    agents = installed_agents()
    if not agents:
        return ""
    lines = ["## Installed agents (scheduled, autonomous)"]
    for a in agents[: _CAPS["agents"]]:
        status = "enabled" if a.get("enabled", True) else "PAUSED"
        sched = _human_schedule(a.get("kind", ""), a.get("when", ""))
        deliver = a.get("deliver_to") or "log"
        last = a.get("last_fired") or "never"
        lines.append(
            f"- **{a['name']}** [{status}] · {sched} · → {deliver} · "
            f"last: {last}"
        )
    if len(agents) > _CAPS["agents"]:
        lines.append(f"- … and {len(agents) - _CAPS['agents']} more")
    return "\n".join(lines)


def _swarms_section() -> str:
    specs = installed_swarms()
    if not specs:
        return ""
    lines = ["## Installed swarm specs"]
    for s in specs[: _CAPS["swarms"]]:
        lines.append(f"- **{s['name']}** — {s.get('description', '')[:120]}")
    if len(specs) > _CAPS["swarms"]:
        lines.append(f"- … and {len(specs) - _CAPS['swarms']} more")
    return "\n".join(lines)


def _skills_summary_section() -> str:
    by_state = skill_counts_by_state()
    total = sum(by_state.values())
    if total == 0:
        return ""
    parts = [
        f"{n} {state}" for state, n in by_state.items() if n
    ]
    return (
        "## Installed skills\n\n"
        f"{total} total: " + ", ".join(parts) +
        " (use `/skills` to list)"
    )


def _recent_fires_section() -> str:
    fires = recent_fires(_CAPS["recent_fires"])
    if not fires:
        return ""
    lines = ["## Recent agent fires (most recent first)"]
    for f in fires:
        lines.append(
            f"- {f['fired_at']} — **{f['name']}** "
            f"({f.get('output_preview', '')[:100]})"
        )
    return "\n".join(lines)


# ---------- Filesystem introspection ----------


def installed_agents() -> list[dict[str, Any]]:
    """Pair each ~/.janus/triggers/<name>.yaml with ~/.janus/skills/<name>.md.

    A trigger without a matching skill is a "raw trigger" (pre-v1.6
    style) — not surfaced as an agent. A skill without a trigger is just
    a skill — also not surfaced here.
    """
    if not config.TRIGGERS_DIR.is_dir():
        return []
    state = _read_daemon_state()
    out: list[dict[str, Any]] = []
    for trig_path in sorted(config.TRIGGERS_DIR.glob("*.yaml")):
        name = trig_path.stem
        skill_path = config.SKILLS_DIR / f"{name}.md"
        if not skill_path.is_file():
            continue
        meta = _parse_trigger_yaml(trig_path)
        if not meta:
            continue
        meta["name"] = name
        meta["last_fired"] = state.get(name)
        out.append(meta)
    return out


def installed_swarms() -> list[dict[str, Any]]:
    if not config.SWARM_SPECS_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for spec_path in sorted(config.SWARM_SPECS_DIR.glob("*.md")):
        try:
            text = spec_path.read_text(encoding="utf-8")[:2000]
        except OSError:
            continue
        desc = ""
        for line in text.splitlines():
            if line.strip().startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip('"\'')
                break
        out.append({"name": spec_path.stem, "description": desc})
    return out


def skill_counts_by_state() -> dict[str, int]:
    """Count installed skill files grouped by lifecycle state.

    Reads the `state:` field from each skill's frontmatter. Cheap; we
    only read the first ~600 bytes per file.
    """
    out: dict[str, int] = {}
    if not config.SKILLS_DIR.is_dir():
        return out
    for skill_path in config.SKILLS_DIR.glob("*.md"):
        st = _peek_skill_state(skill_path)
        if st:
            out[st] = out.get(st, 0) + 1
    return out


def recent_fires(n: int = 10) -> list[dict[str, Any]]:
    """Walk ~/.janus/cron/output/ for the last N fires across all agents.

    Each archive file is named with an ISO-8601 timestamp (colons → '-')
    so we sort by filename for chronology. Includes a short preview of
    the output body.
    """
    archive = config.HOME / "cron" / "output"
    if not archive.is_dir():
        return []
    candidates: list[tuple[str, Path, str]] = []  # (sort_key, path, agent_name)
    for agent_dir in archive.iterdir():
        if not agent_dir.is_dir():
            continue
        for f in agent_dir.glob("*.md"):
            candidates.append((f.stem, f, agent_dir.name))
    candidates.sort(reverse=True)  # newest first
    out: list[dict[str, Any]] = []
    for sort_key, path, agent_name in candidates[:n]:
        preview = ""
        try:
            text = path.read_text(encoding="utf-8")
            # Skip YAML frontmatter (between two `---` lines), then find
            # the first non-empty body line. We count separators rather
            # than toggling — the FIRST `---` opens, the SECOND closes.
            lines = text.splitlines()
            sep_count = 0
            past_fm = not text.startswith("---")
            for line in lines:
                if not past_fm:
                    if line.strip() == "---":
                        sep_count += 1
                        if sep_count == 2:
                            past_fm = True
                    continue
                if line.strip():
                    preview = line.strip()
                    break
        except OSError:
            continue
        out.append({
            "name": agent_name,
            "fired_at": sort_key.replace("-", ":", 2)[:19],  # crude reverse
            "output_preview": preview,
            "path": str(path),
        })
    return out


# ---------- Tiny readers ----------


def _read_daemon_state() -> dict[str, Any]:
    if not config.DAEMON_STATE.is_file():
        return {}
    try:
        return json.loads(config.DAEMON_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _parse_trigger_yaml(path: Path) -> dict[str, Any] | None:
    """Hand-rolled key:value reader. Same subset our YAML parser supports.

    We don't need full YAML — triggers are flat key:value files written
    by agent_create. Returns None if the file is unreadable.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if ":" not in line or line.startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        if val == "true":
            val = True
        elif val == "false":
            val = False
        elif val.isdigit():
            val = int(val)
        out[key] = val
    return out


def _peek_skill_state(path: Path) -> str | None:
    """Read just the `state:` line from a skill file's frontmatter."""
    try:
        with path.open("r", encoding="utf-8") as f:
            head = f.read(800)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    for line in head.splitlines()[1:30]:
        if line.strip() == "---":
            break
        if line.strip().startswith("state:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return None


def _human_schedule(kind: str, when: str) -> str:
    if kind == "interval":
        try:
            secs = int(when)
        except (TypeError, ValueError):
            return f"interval:{when}"
        if secs % 86400 == 0:
            return f"every {secs // 86400}d"
        if secs % 3600 == 0:
            return f"every {secs // 3600}h"
        if secs % 60 == 0:
            return f"every {secs // 60}min"
        return f"every {secs}s"
    if kind == "cron":
        return f"cron `{when}`"
    return f"{kind}:{when}"


# ---------- Search across memory + audit ----------


def search_memory(query: str, top_k: int = 10) -> list[dict[str, Any]]:
    """Substring + word search across every memory category file AND the
    cron-fire memory-write audit log.

    Returns hits sorted by recency-of-file-touch. Each hit includes
    category, section, line number, the matching line, and 2 lines of
    context above/below.
    """
    if not query.strip():
        return []
    query_lc = query.lower()
    paths: list[Path] = []
    if config.MEMORY_DIR.is_dir():
        paths.extend(sorted(config.MEMORY_DIR.glob("*.md")))
    audit_dir = config.MEMORY_DIR / "_audit"
    if audit_dir.is_dir():
        paths.extend(sorted(audit_dir.glob("*.md"), reverse=True))
    hits: list[dict[str, Any]] = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        section = ""
        for i, line in enumerate(lines):
            if line.startswith("## "):
                section = line[3:].strip()
            if query_lc in line.lower():
                ctx_above = "\n".join(lines[max(0, i - 1):i])
                ctx_below = "\n".join(lines[i + 1:min(len(lines), i + 2)])
                hits.append({
                    "category": p.stem if p.parent == config.MEMORY_DIR else f"_audit/{p.stem}",
                    "section": section,
                    "line_no": i + 1,
                    "line": line.strip(),
                    "context_above": ctx_above,
                    "context_below": ctx_below,
                    "path": str(p),
                })
            if len(hits) >= top_k:
                return hits
    return hits
