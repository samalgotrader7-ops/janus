"""
conversation.py — persistent session state (Phase 13).

WHY:
A turn shouldn't be the unit of restart. If the user closes the CLI
mid-task or comes back tomorrow, they should be able to pick up where
they left off. log.jsonl is an append-only audit trail; it isn't
resumable. This module stores the rolling per-session state that lets
`--continue` and `--resume <id>` work.

MODEL:
A Conversation is a list of completed turns plus metadata. Each turn
records the user's request, the chosen interpretation, the agent's
output, and a timestamp. The LLM does NOT receive the raw turn list
(too noisy and would blow the context); it receives a brief recap
block via `recent_context_block()`.

`/compact` runs a small LLM pass to summarize the older turns into a
single one-paragraph "earlier in this session" line, then drops the
detailed turns. Used to keep the recap block small even on long
sessions.

FILES:
~/.janus/conversations/<id>.json — JSON, the user can `cat`/`jq`/edit.

THIS IS NOT chat-history-as-message-list:
The executor still builds a fresh messages list per turn (P1, P9 — no
hidden state in the agent loop). Conversation only adds a memory layer
ABOVE the executor. Each turn is still independently auditable.
"""

from __future__ import annotations
import datetime
import json
import secrets
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import config, llm


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    """ISO-8601 timestamp + 4-char hex suffix. Sortable, unique."""
    return _iso_now().replace(":", "-") + "-" + secrets.token_hex(2)


@dataclass
class Conversation:
    id: str
    started: str
    last_updated: str
    model: str
    workspace: str
    turns: list[dict] = field(default_factory=list)
    summary: str = ""  # populated by /compact
    # v1.9.0: short auto-generated label (3-6 words). Populated by
    # title_generator after the first turn completes. Surfaced by
    # `/conversations`, `--resume` picker, `/insights` recent_titles.
    title: str = ""

    # ---- mutators ----

    def add_turn(self, *, request: str, output: str, choice=None,
                 skill: str | None = None, ts: str | None = None) -> None:
        self.turns.append({
            "ts": ts or _iso_now(),
            "request": request,
            "output": output,
            "choice": choice,
            "skill": skill,
        })
        self.last_updated = _iso_now()

    def clear_turns(self) -> None:
        self.turns = []
        self.summary = ""
        self.last_updated = _iso_now()

    # ---- view helpers ----

    def recent_context_block(self, k: int | None = None) -> str:
        """Prepended to the interpreter's memory_preamble each turn so the
        LLM has light awareness of recent context. Capped at `k` turns
        (default: config.CONVERSATION_RECAP_TURNS) plus the compaction
        summary if present."""
        if k is None:
            k = config.CONVERSATION_RECAP_TURNS
        if not self.turns and not self.summary:
            return ""
        parts = []
        if self.summary:
            parts.append("# Earlier in this session\n\n" + self.summary)
        recent = self.turns[-k:] if k > 0 else []
        if recent:
            parts.append("# Recent turns (most recent last)")
            for i, t in enumerate(recent, 1):
                req = (t.get("request") or "").strip().splitlines()[0][:120]
                out_lines = (t.get("output") or "").strip().splitlines()
                first = out_lines[0][:120] if out_lines else ""
                parts.append(f"  {i}. user: {req}")
                if first:
                    parts.append(f"     agent: {first}")
        return "\n\n".join(parts) + "\n\n---\n"


# ---------- File I/O ----------


def _path(id: str) -> Path:
    config.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    return config.CONVERSATIONS_DIR / f"{id}.json"


def save(c: Conversation) -> None:
    _path(c.id).write_text(
        json.dumps(asdict(c), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load(id: str) -> Conversation | None:
    p = _path(id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return Conversation(
        id=str(d.get("id", id)),
        started=str(d.get("started", "")),
        last_updated=str(d.get("last_updated", "")),
        model=str(d.get("model", "")),
        workspace=str(d.get("workspace", "")),
        turns=list(d.get("turns") or []),
        summary=str(d.get("summary", "")),
        title=str(d.get("title", "")),
    )


def list_all() -> list[dict]:
    """Conversation summaries, newest-first. Caller-friendly for `/resume`
    pickers."""
    config.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for p in config.CONVERSATIONS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "id": d.get("id", p.stem),
            "started": d.get("started", ""),
            "last_updated": d.get("last_updated", ""),
            "model": d.get("model", ""),
            "turns": len(d.get("turns") or []),
            "title": d.get("title", ""),
        })
    out.sort(key=lambda x: x.get("last_updated", ""), reverse=True)
    return out


def latest() -> Conversation | None:
    """Most recently updated conversation, for `--continue`."""
    items = list_all()
    if not items:
        return None
    return load(items[0]["id"])


# ---------- --continue / --resume hand-off ----------
#
# Set by __main__.py based on CLI flags; consumed by cli.py / cli_rich.py
# at session boot. Lets us pass a Conversation across module boundaries
# without smuggling it through `sys.argv`.

_PENDING: "Conversation | None" = None


def set_pending(conv: "Conversation") -> None:
    global _PENDING
    _PENDING = conv


def take_pending() -> "Conversation | None":
    global _PENDING
    out = _PENDING
    _PENDING = None
    return out


def new(model: str = "", workspace: str = "") -> Conversation:
    """Fresh conversation, NOT yet persisted. Save it after the first
    turn so empty conversations don't clutter the directory."""
    ts = _iso_now()
    return Conversation(
        id=new_id(),
        started=ts,
        last_updated=ts,
        model=model or config.MODEL,
        workspace=workspace or str(config.WORKSPACE),
        turns=[],
        summary="",
    )


# ---------- Compaction ----------


_COMPACT_SYSTEM = """You summarize a Janus session for the agent's own memory.

You will receive a sequence of (user request, agent output) pairs from
earlier in the session. Produce ONE prose paragraph (3-6 sentences) that
captures:
- what the user has been working on
- key decisions made or facts established
- anything the agent should remember for future turns in this session

Be terse. The user is going to skim this. No preamble, no markdown
headers. Plain prose only."""


def compact(c: Conversation, *, keep_last: int = 3) -> Conversation:
    """Replace older turns with a summary. Keeps the most recent
    `keep_last` turns intact so the agent doesn't lose immediate context.
    Idempotent on a session with ≤ keep_last turns."""
    if len(c.turns) <= keep_last:
        return c

    older = c.turns[:-keep_last]
    transcript = "\n\n".join(
        f"USER: {(t.get('request') or '').strip()[:500]}\n"
        f"AGENT: {(t.get('output') or '').strip()[:500]}"
        for t in older
    )
    if c.summary:
        transcript = f"PRIOR SUMMARY: {c.summary}\n\n{transcript}"

    try:
        msg = llm.chat(
            messages=[
                {"role": "system", "content": _COMPACT_SYSTEM},
                {"role": "user", "content": transcript},
            ],
            temperature=0.2,
        )
        new_summary = (msg.get("content") or "").strip()
    except Exception as e:
        new_summary = c.summary + f"\n[compaction failed: {type(e).__name__}: {e}]"

    if new_summary:
        c.summary = new_summary
        c.turns = c.turns[-keep_last:]
        c.last_updated = _iso_now()
    return c
