"""
tool_schema_slim.py — heuristic tool-schema slimming (v1.34.9, Phase 9.5).

WHY:
The full bundled tool registry has ~50 tools whose JSON schemas
land in every system prompt — roughly 5-7KB of tokens per turn.
For shorter / focused turns this is overhead; the model rarely
calls more than 3-5 distinct tools in any given turn.

THIS MODULE ships a pure-compute scoring function that ranks tools
by likely relevance to the current turn, plus an opt-in env flag
(JANUS_TOOL_SCHEMA_SLIM=1) that, when set, applies the slim list
in executor.chat instead of every-tool. Default OFF — preserves
existing behavior pixel-for-pixel until a user opts in.

HEURISTIC INPUTS:
  * Always-include set: low-cost essentials the model uses across
    domains (fs_read/list/glob/grep, shell, web_fetch, …).
  * Skill-attached: tools listed in any loaded skill's
    `tool_names:` or `capabilities:` keys.
  * Recent-history: tools called in the last N records of the
    current trace (default last 8 turns).

The output ALWAYS includes every always-include + every skill-
attached + every recent. Only the long tail of rarely-used tools
gets dropped. If the slimmed list is empty for some reason, we
fall back to the full registry so the model never gets zero tools.

LIMITS:
  * v1.34.9 is the framework. The default-off flag means no token
    savings unless you opt in. A/B against your usage to validate.
  * Future: model-aware budgets (smaller models benefit more from
    fewer tools), per-skill scoring weights, learning-loop tuning.
"""

from __future__ import annotations

import os
from typing import Iterable


# Always-include set — tools the model needs across domains.
# Picked by usage frequency from log.jsonl analysis. Adjust by PR.
ALWAYS_INCLUDE: frozenset[str] = frozenset({
    "fs_read", "fs_list", "fs_glob", "fs_grep",
    "shell",
    "web_fetch", "web_search",
    "session_recent", "session_search",
    "memory_search",
    "todo_read", "todo_write",
    "clarify",
    "exit_plan_mode",
})


def is_enabled() -> bool:
    """JANUS_TOOL_SCHEMA_SLIM env flag check."""
    return os.environ.get(
        "JANUS_TOOL_SCHEMA_SLIM", "0",
    ).lower() in ("1", "true", "yes", "on")


def collect_skill_tool_names(loaded_skills: Iterable) -> set[str]:
    """Pull tool_names + capability keys from each loaded skill.

    Each skill object is expected to have:
      .tool_names      list[str] | None  — explicit tool list
      .capabilities    dict | None       — capability key like
                                            'shell' or 'mcp.<server>'

    Capability keys with dots (mcp.<server>.<tool>) yield the prefix
    'mcp_<server>_<tool>' to match how MCP tools are exposed in the
    Janus registry."""
    out: set[str] = set()
    for skill in loaded_skills or []:
        names = getattr(skill, "tool_names", None) or []
        for n in names:
            out.add(str(n))
        caps = getattr(skill, "capabilities", None) or {}
        if isinstance(caps, dict):
            for key in caps.keys():
                key_str = str(key)
                if key_str.startswith("mcp."):
                    parts = key_str.split(".", 2)
                    if len(parts) >= 2:
                        server = parts[1]
                        # Without a specific tool, we can't predict
                        # which mcp_<server>_* the model will call.
                        # Skip — recent history will catch the
                        # actual usage.
                        if len(parts) == 3:
                            tool = parts[2].replace("-", "_")
                            out.add(f"mcp_{server}_{tool}")
                else:
                    out.add(key_str)
    return out


def collect_recent_tool_names(
    recent_records: Iterable[dict],
    *,
    window: int = 8,
) -> set[str]:
    """Extract tool names from the last `window` trace records that
    contain a 'tool' or 'tool_name' field."""
    seq = list(recent_records or [])[-window:]
    out: set[str] = set()
    for r in seq:
        if not isinstance(r, dict):
            continue
        name = r.get("tool") or r.get("tool_name")
        if name:
            out.add(str(name))
    return out


def select_relevant(
    all_schemas: list[dict],
    *,
    loaded_skills: Iterable | None = None,
    recent_records: Iterable[dict] | None = None,
) -> list[dict]:
    """Return the slim subset of schemas relevant to the current turn.

    Falls back to ALL schemas when:
      * the slim env flag is OFF (caller may bypass slimming
        entirely; this fallback covers the path where this function
        is called directly)
      * the computed slim set is empty (defensive)

    The function is pure — same inputs → same output."""
    if not all_schemas:
        return []
    if not is_enabled():
        return list(all_schemas)

    relevant_names: set[str] = set(ALWAYS_INCLUDE)
    if loaded_skills:
        relevant_names |= collect_skill_tool_names(loaded_skills)
    if recent_records:
        relevant_names |= collect_recent_tool_names(recent_records)

    out: list[dict] = []
    for s in all_schemas:
        # Tool schemas use the OpenAI function shape:
        #   {"type": "function", "function": {"name": "...", ...}}
        name = ""
        try:
            if isinstance(s, dict):
                fn = s.get("function") or {}
                name = str(fn.get("name") or s.get("name") or "")
        except Exception:
            name = ""
        if name and name in relevant_names:
            out.append(s)

    if not out:
        # Defense — never starve the model of tools.
        return list(all_schemas)
    return out
