"""
skill_proposer.py — auto-detect recurring patterns + draft skills (v1.28.0).

The Janus brand is "Claude Code's UX, on any model, with a learning
loop." v1.28.0 lights up the **learning loop** half. Pre-1.28 the
agent had skills + memory but no mechanism to PROPOSE new skills
from observed work. Users had to write them by hand.

This module adds pure-compute pattern detection over the session's
trace + recent log.jsonl entries. Detected patterns are surfaced as
one-line offers ("I noticed you've fs_read+fs_edit+pytest a few
times — want me to draft a skill?"). The user opts in; an LLM pass
(``skills.draft_skill_from_log``) writes the skill spec as a
``quarantined`` markdown file in ``~/.janus/skills/``. The user
``/promote``-s it manually — same trust ladder Janus has had since
v1.0 (P4 invariant: no auto-promotion).

DESIGN CHOICES:

  * **Pure-compute detection.** Pattern recognition runs as regex /
    counter passes over already-recorded trace + log entries.
    Cheap, deterministic, no token spend. The LLM only runs when
    the user explicitly opts into drafting.

  * **No automatic LLM-pass.** A v1.28.0 release that secretly
    fired LLM calls in the background after every turn would
    surprise users with token spend. Drafting is gated behind
    ``/skills propose <id>``.

  * **Cooldown'd offers.** A detected pattern that the user
    declined or already saw recently goes silent for 7 days
    (``JANUS_SKILL_OFFER_COOLDOWN_DAYS``). Persistent state at
    ``~/.janus/skills/_proposals_state.json``. Same shape as the
    inferred-memory cooldowns from v1.19.0.

  * **Three pattern kinds (v1.28.0):**

    - ``repeated_tool_sequence`` — the same length-N tool sequence
      ran ≥3 times across recent turns (with sequence length 2-4).
      Strongest signal of a routine.

    - ``repeated_file`` — the same file path appeared in ≥4 tool
      calls over the recent window. Suggests "I keep doing things
      to this file, maybe a per-file skill."

    - ``repeated_shape`` — the same coarse tool-class shape ran
      ≥3 times (e.g. ``[search] [edit] [verify]``). Generalizes
      across files. Useful for "how to do X" skills.

  * **No detection in headless / CI.** Pattern offers belong in
    interactive sessions where the user can accept/decline. The
    detection module is callable but the surfaces (cli_rich) only
    invoke it in the chat loop.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config


# ---------- Tunables (env-overridable) ----------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Sequence-length window for repeated-tool-sequence detection.
SEQ_MIN_LEN = 2
SEQ_MAX_LEN = 4

# How many times a pattern must repeat before it's an offer.
SEQ_MIN_OCCURRENCES = 3
FILE_MIN_OCCURRENCES = 4
SHAPE_MIN_OCCURRENCES = 3

# How many recent log entries to consider.
DEFAULT_HISTORY_DEPTH = 30

# Cooldown after a user declines or sees a pattern offer.
COOLDOWN_DAYS = _env_int("JANUS_SKILL_OFFER_COOLDOWN_DAYS", 7)

# v1.28.1 — auto-offer threshold. cli_rich's after-turn offer fires
# only when the top pattern hit ≥ this many occurrences. Higher than
# detection thresholds because drafting costs an LLM call — we want a
# strong signal before nudging.
AUTO_OFFER_MIN_OCCURRENCES = _env_int(
    "JANUS_SKILL_AUTO_OFFER_MIN_OCCURRENCES", 4,
)


# ---------- Pattern dataclass ----------


@dataclass
class Pattern:
    """A detected recurring pattern.

    ``id`` is a stable hash so the same pattern surfaced across
    sessions is recognizable (and respects cooldowns).
    """
    id: str
    kind: str  # "repeated_tool_sequence" / "repeated_file" / "repeated_shape"
    description: str
    occurrences: int
    detail: dict[str, Any] = field(default_factory=dict)


# ---------- Tool classification (for shape patterns) ----------


_TOOL_CLASS = {
    # Search / read
    "fs_read": "read",
    "fs_list": "read",
    "fs_grep": "search",
    "fs_glob": "search",
    "memory_search": "search",
    "session_search": "search",
    "session_recent": "search",
    "web_search": "search",
    "web_fetch": "read",
    # Edit / write
    "fs_write": "edit",
    "fs_edit": "edit",
    "fs_multi_edit": "edit",
    "todo_write": "edit",
    # Exec / verify
    "shell": "exec",
    "shell_run_bg": "exec",
    "shell_output": "read",
    "shell_kill": "exec",
    "code_exec_python": "exec",
    "ssh_exec": "exec",
    # Agent meta
    "subagent": "delegate",
    "delegate": "delegate",
    "swarm_run": "delegate",
    "agent_create": "create-agent",
    "exit_plan_mode": "plan",
}


def _tool_class(name: str) -> str:
    """Coarse class for a tool name. 'other' for anything unknown."""
    return _TOOL_CLASS.get(name, "other")


# ---------- Trace extraction ----------


def _extract_tool_calls(trace: list[dict] | None) -> list[dict]:
    """Pull (tool_name, path) tuples from a trace list, skipping
    refusals/errors. ``trace`` is the per-turn step list emitted by
    executor.chat."""
    out: list[dict] = []
    if not trace:
        return out
    for step in trace:
        if not isinstance(step, dict):
            continue
        if step.get("type") not in ("tool_call", "tool_result"):
            continue
        # Use only tool_call entries to avoid double-counting.
        if step.get("type") != "tool_call":
            continue
        name = step.get("tool")
        if not name:
            continue
        args = step.get("args") or {}
        path = (
            args.get("path")
            or args.get("command")
            or args.get("query")
            or args.get("url")
            or ""
        )
        out.append({"tool": str(name), "path": str(path)})
    return out


def _gather_recent_calls(
    *,
    current_trace: list[dict] | None = None,
    history_depth: int = DEFAULT_HISTORY_DEPTH,
) -> list[dict]:
    """Combine the current turn's trace with the last N log entries
    that include a trace. Returns a flat list of {tool, path} dicts."""
    calls: list[dict] = []

    # Last N log entries with traces. log.jsonl entries from the chat
    # loop carry a ``trace`` key for full turns; not every entry has one.
    try:
        from . import logger as _logger
        records = _logger.read_all()
    except Exception:
        records = []

    # Scan from newest backwards, take up to history_depth turn-shaped
    # records. Prepend (older first) so the time-order is right.
    relevant: list[list[dict]] = []
    for rec in reversed(records):
        tr = rec.get("trace")
        if isinstance(tr, list) and tr:
            relevant.append(tr)
            if len(relevant) >= history_depth:
                break
    for tr in reversed(relevant):
        calls.extend(_extract_tool_calls(tr))

    # Then current turn (most recent).
    if current_trace:
        calls.extend(_extract_tool_calls(current_trace))

    return calls


# ---------- Detection: repeated tool sequences ----------


def _slug(text: str, *, maxlen: int = 60) -> str:
    """Lowercase / dash-separated / safe-for-filename slug."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen] or "pattern"


def _seq_pattern_id(tools: tuple[str, ...]) -> str:
    return "seq-" + _slug("-".join(tools))


def _file_pattern_id(path: str) -> str:
    return "file-" + _slug(path)


def _shape_pattern_id(classes: tuple[str, ...]) -> str:
    return "shape-" + _slug("-".join(classes))


def _detect_repeated_sequences(calls: list[dict]) -> list[Pattern]:
    """Find length-N tool-name subsequences that repeat ≥SEQ_MIN_OCCURRENCES."""
    out: list[Pattern] = []
    if len(calls) < SEQ_MIN_LEN * SEQ_MIN_OCCURRENCES:
        return out
    tool_names = [c["tool"] for c in calls]

    seen_ids: set[str] = set()  # dedupe — short seq subsumed by longer
    # Iterate longer first so the most-specific pattern wins.
    for length in range(SEQ_MAX_LEN, SEQ_MIN_LEN - 1, -1):
        if len(tool_names) < length:
            continue
        counts: dict[tuple[str, ...], int] = {}
        for i in range(len(tool_names) - length + 1):
            seq = tuple(tool_names[i:i + length])
            counts[seq] = counts.get(seq, 0) + 1
        for seq, count in counts.items():
            if count < SEQ_MIN_OCCURRENCES:
                continue
            pid = _seq_pattern_id(seq)
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            out.append(Pattern(
                id=pid,
                kind="repeated_tool_sequence",
                description=(
                    f"Tool sequence '{' → '.join(seq)}' "
                    f"appeared {count} times"
                ),
                occurrences=count,
                detail={"sequence": list(seq), "length": length},
            ))
    return out


# ---------- Detection: repeated file ----------


def _detect_repeated_files(calls: list[dict]) -> list[Pattern]:
    counts: dict[str, int] = {}
    for c in calls:
        path = c.get("path") or ""
        # Filter out non-file paths — shell commands, URLs, queries
        # would inflate the counter.
        if not path:
            continue
        if "://" in path:  # URL
            continue
        if path.startswith(("http", "ssh ", "git ", "npm ")):
            continue
        if " " in path:  # likely a shell command, not a path
            continue
        counts[path] = counts.get(path, 0) + 1

    out: list[Pattern] = []
    for path, count in counts.items():
        if count < FILE_MIN_OCCURRENCES:
            continue
        out.append(Pattern(
            id=_file_pattern_id(path),
            kind="repeated_file",
            description=(
                f"File '{path}' touched {count} times"
            ),
            occurrences=count,
            detail={"path": path},
        ))
    # Newest highest-count first
    out.sort(key=lambda p: -p.occurrences)
    return out


# ---------- Detection: repeated shape ----------


def _detect_repeated_shapes(calls: list[dict]) -> list[Pattern]:
    """Coarse class sequences. e.g. [search, edit, exec] = "find,
    change, verify" — a recognizable shape across files."""
    if len(calls) < SHAPE_MIN_OCCURRENCES * 2:
        return []
    classes = [_tool_class(c["tool"]) for c in calls]

    out: list[Pattern] = []
    seen: set[str] = set()
    # Lengths 2-4, drop 'other' so noise doesn't pollute shapes.
    filtered = [c for c in classes if c != "other"]
    for length in range(SEQ_MAX_LEN, SEQ_MIN_LEN - 1, -1):
        if len(filtered) < length:
            continue
        counts: dict[tuple[str, ...], int] = {}
        for i in range(len(filtered) - length + 1):
            shape = tuple(filtered[i:i + length])
            # Skip degenerate shapes (all-same-class)
            if len(set(shape)) < 2:
                continue
            counts[shape] = counts.get(shape, 0) + 1
        for shape, count in counts.items():
            if count < SHAPE_MIN_OCCURRENCES:
                continue
            pid = _shape_pattern_id(shape)
            if pid in seen:
                continue
            seen.add(pid)
            out.append(Pattern(
                id=pid,
                kind="repeated_shape",
                description=(
                    f"Shape '{' → '.join(shape)}' "
                    f"appeared {count} times across files"
                ),
                occurrences=count,
                detail={"shape": list(shape), "length": length},
            ))
    return out


# ---------- Public API ----------


def detect(
    *,
    current_trace: list[dict] | None = None,
    history_depth: int = DEFAULT_HISTORY_DEPTH,
) -> list[Pattern]:
    """Detect all pattern types over current trace + recent log.

    Returns patterns sorted by signal strength (occurrences DESC).
    Empty list if nothing meets the thresholds.
    """
    calls = _gather_recent_calls(
        current_trace=current_trace,
        history_depth=history_depth,
    )
    if not calls:
        return []
    out: list[Pattern] = []
    out.extend(_detect_repeated_sequences(calls))
    out.extend(_detect_repeated_files(calls))
    out.extend(_detect_repeated_shapes(calls))
    out.sort(key=lambda p: -p.occurrences)
    return out


# ---------- Cooldown / proposal-state persistence ----------


def _state_path() -> Path:
    return config.SKILLS_DIR / "_proposals_state.json"


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    config.ensure_home()
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_in_cooldown(pattern_id: str) -> bool:
    """True if this pattern was offered/declined within the cooldown
    window. Used to suppress re-offering."""
    state = _load_state()
    entry = state.get(pattern_id) or {}
    last = entry.get("last_offered") or entry.get("declined_at")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    age = (datetime.now(timezone.utc) - last_dt).days
    return age < COOLDOWN_DAYS


def mark_offered(pattern_id: str) -> None:
    state = _load_state()
    entry = state.get(pattern_id) or {}
    entry["last_offered"] = _now_iso()
    entry["offer_count"] = (entry.get("offer_count") or 0) + 1
    state[pattern_id] = entry
    _save_state(state)


def mark_declined(pattern_id: str) -> None:
    state = _load_state()
    entry = state.get(pattern_id) or {}
    entry["declined_at"] = _now_iso()
    entry["decline_count"] = (entry.get("decline_count") or 0) + 1
    state[pattern_id] = entry
    _save_state(state)


def mark_accepted(pattern_id: str) -> None:
    """Record acceptance so we don't keep re-offering the same pattern
    after a skill has already been drafted."""
    state = _load_state()
    entry = state.get(pattern_id) or {}
    entry["accepted_at"] = _now_iso()
    state[pattern_id] = entry
    _save_state(state)


def filter_offerable(patterns: list[Pattern]) -> list[Pattern]:
    """Drop patterns that are in cooldown or already accepted."""
    state = _load_state()
    out = []
    for p in patterns:
        entry = state.get(p.id) or {}
        if entry.get("accepted_at"):
            continue
        if is_in_cooldown(p.id):
            continue
        out.append(p)
    return out


# ---------- Drafting (LLM-gated, opt-in) ----------


def _pattern_to_log_records(pattern: Pattern, calls: list[dict]) -> list[dict]:
    """Build a minimal log-record list for skills.draft_skill_from_log
    that focuses on the pattern's tool calls."""
    seq = pattern.detail.get("sequence")
    path = pattern.detail.get("path")
    shape = pattern.detail.get("shape")
    matched_calls: list[dict] = []
    if seq:
        # Find each occurrence of the sequence
        seq_t = list(seq)
        for i in range(len(calls) - len(seq_t) + 1):
            window = [c["tool"] for c in calls[i:i + len(seq_t)]]
            if window == seq_t:
                matched_calls.extend(calls[i:i + len(seq_t)])
    elif path:
        matched_calls = [c for c in calls if c.get("path") == path]
    elif shape:
        # Skip shape-based excerpting; pass everything for context
        matched_calls = list(calls)
    if not matched_calls:
        matched_calls = list(calls)

    # Wrap as a single fake record for draft_skill_from_log.
    return [{
        "request": pattern.description,
        "trace": [
            {"type": "tool_call", "tool": c["tool"], "args": {"path": c.get("path", "")}}
            for c in matched_calls
        ],
        "output": "",
        "feedback": None,
    }]


def draft_skill(pattern: Pattern, *, current_trace: list[dict] | None = None) -> Path:
    """Run the existing skills.draft_skill_from_log LLM pass on the
    pattern + observed calls, write the draft as a quarantined skill.

    This is the ONLY place this module makes an LLM call. Caller must
    have user opt-in.

    Returns the path the draft was written to.
    """
    from . import skills as _skills  # lazy — skills imports llm
    calls = _gather_recent_calls(current_trace=current_trace)
    log_records = _pattern_to_log_records(pattern, calls)
    draft = _skills.draft_skill_from_log(pattern.description, log_records)
    if not isinstance(draft, dict) or not draft.get("name"):
        # LLM didn't return a usable draft — fabricate a minimal one
        # from the pattern description so the user sees something
        # rather than a silent no-op.
        draft = {
            "name": pattern.id,
            "description": pattern.description,
            "body": (
                f"# {pattern.description}\n\n"
                "(LLM draft failed — fill in the procedure here)"
            ),
            "capabilities": {},
        }
    path = _skills.write_draft(draft)
    mark_accepted(pattern.id)
    return path


# ---------- Helpers for the slash command ----------


def format_offer_line(pattern: Pattern) -> str:
    """One-line user-facing offer string for cli_rich rendering."""
    return (
        f"{pattern.description}. "
        f"Run /skills propose {pattern.id} to draft a skill."
    )


def list_offerable(*, current_trace: list[dict] | None = None) -> list[Pattern]:
    """Top-level helper for the slash command. Detects, filters by
    cooldown / accepted, returns the offerable patterns."""
    return filter_offerable(detect(current_trace=current_trace))


__all__ = [
    "Pattern",
    "detect",
    "filter_offerable",
    "is_in_cooldown",
    "mark_offered",
    "mark_declined",
    "mark_accepted",
    "draft_skill",
    "format_offer_line",
    "list_offerable",
    "SEQ_MIN_OCCURRENCES",
    "FILE_MIN_OCCURRENCES",
    "SHAPE_MIN_OCCURRENCES",
    "COOLDOWN_DAYS",
    "AUTO_OFFER_MIN_OCCURRENCES",
]
