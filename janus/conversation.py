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
    # v1.12.0 — manual compression feedback. List of turn indexes (into
    # `turns`) that the user pinned via /pin. compact() skips these
    # when summarizing — they survive the rolling window. Indexes shift
    # automatically when compaction prunes older non-pinned turns.
    pinned_turns: list[int] = field(default_factory=list)
    # v1.27.3 — gateway origin tag (cli_rich / cli / telegram / web /
    # whatsapp / tui / headless). Populated by ``new(gateway=...)``
    # at conversation creation time. The /resume picker filters by
    # this field. Empty string for legacy conversations created
    # pre-v1.27.3 (load defaults to "").
    gateway: str = ""

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
        pinned_turns=list(d.get("pinned_turns") or []),
        # v1.27.3: legacy conversations have no gateway field — default
        # to empty string so the /resume picker shows "-" rather than
        # crashing.
        gateway=str(d.get("gateway", "")),
    )


def list_all() -> list[dict]:
    """Conversation summaries, newest-first. Caller-friendly for `/resume`
    pickers.

    v1.27.3: each entry now also carries:
      * ``gateway`` — origin tag (cli_rich / telegram / web / etc.)
      * ``first_user_msg`` — first user turn's request, truncated
      * ``last_user_msg`` — most recent user turn, truncated
      * ``last_assistant_msg`` — most recent assistant output, truncated
    Used by the upgraded ``/resume`` picker to render previews
    without re-loading every conversation file.
    """
    config.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for p in config.CONVERSATIONS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        turns = d.get("turns") or []
        first_user = ""
        last_user = ""
        last_assistant = ""
        if turns:
            first_user = (turns[0].get("request") or "")[:120].strip()
            # Walk backwards for the most recent user/assistant snippets
            for t in reversed(turns):
                if not last_user:
                    last_user = (t.get("request") or "")[:120].strip()
                if not last_assistant:
                    last_assistant = (t.get("output") or "")[:160].strip()
                if last_user and last_assistant:
                    break
        out.append({
            "id": d.get("id", p.stem),
            "started": d.get("started", ""),
            "last_updated": d.get("last_updated", ""),
            "model": d.get("model", ""),
            "turns": len(turns),
            "title": d.get("title", ""),
            "gateway": d.get("gateway", ""),
            "first_user_msg": first_user,
            "last_user_msg": last_user,
            "last_assistant_msg": last_assistant,
        })
    out.sort(key=lambda x: x.get("last_updated", ""), reverse=True)
    return out


def latest() -> Conversation | None:
    """Most recently updated conversation, for `--continue`."""
    items = list_all()
    if not items:
        return None
    return load(items[0]["id"])


def search(
    query: str = "",
    *,
    gateway: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Filter ``list_all`` by query / gateway / since-date.

    v1.27.3 — used by the upgraded ``/resume`` picker:

      * ``query`` — case-insensitive substring match against title +
        first user message + last user message + last assistant
        message. Empty string = no query filter.
      * ``gateway`` — exact match on the gateway tag (cli_rich,
        telegram, web, ...). None = no gateway filter.
      * ``since`` — ISO date string (e.g. "2026-05-01" or
        "2026-05-07T12:00:00"). Conversations with
        ``last_updated >= since`` pass. Lexicographic comparison —
        works because ISO 8601 sorts correctly as text.

    Returns a list of summary dicts (same shape as ``list_all``),
    newest-first. Empty list if nothing matches.
    """
    items = list_all()
    q = (query or "").strip().lower()
    if q:
        def _matches(item: dict) -> bool:
            haystack = " ".join([
                str(item.get("title") or ""),
                str(item.get("first_user_msg") or ""),
                str(item.get("last_user_msg") or ""),
                str(item.get("last_assistant_msg") or ""),
                str(item.get("id") or ""),
            ]).lower()
            return q in haystack
        items = [i for i in items if _matches(i)]
    if gateway:
        gw = str(gateway).strip().lower()
        items = [
            i for i in items
            if str(i.get("gateway") or "").strip().lower() == gw
        ]
    if since:
        s = str(since).strip()
        items = [
            i for i in items
            if str(i.get("last_updated") or "") >= s
        ]
    return items


def resolve_target(target: str) -> "Conversation | None":
    """Resolve a /resume argument to a Conversation.

    v1.27.3 accepts:
      * Numeric index (1-based) into the most-recent list — "1" =
        latest, "2" = second-most-recent, etc.
      * Exact id (full ISO timestamp + suffix).
      * Id prefix (matches one of the items uniquely).

    Returns the Conversation, or None if the target doesn't resolve.
    """
    target = (target or "").strip()
    if not target:
        return None

    # Numeric index path
    if target.isdigit():
        idx = int(target)
        items = list_all()
        if 1 <= idx <= len(items):
            return load(items[idx - 1]["id"])
        return None

    # Exact match
    direct = load(target)
    if direct is not None:
        return direct

    # Prefix match — must be unique
    items = list_all()
    matches = [i for i in items if i["id"].startswith(target)]
    if len(matches) == 1:
        return load(matches[0]["id"])
    return None


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


def new(
    model: str = "",
    workspace: str = "",
    *,
    gateway: str = "",
) -> Conversation:
    """Fresh conversation, NOT yet persisted. Save it after the first
    turn so empty conversations don't clutter the directory.

    v1.27.3: ``gateway`` keyword tags the origin (cli_rich / telegram /
    web / etc.) for the /resume picker's filter. Defaults to empty
    string for legacy callers that don't pass it.
    """
    ts = _iso_now()
    return Conversation(
        id=new_id(),
        started=ts,
        last_updated=ts,
        model=model or config.MODEL,
        workspace=workspace or str(config.WORKSPACE),
        turns=[],
        summary="",
        gateway=gateway,
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
    """Replace older turns with a summary.

    v1.12.0: respects `c.pinned_turns`. A pinned turn is NEVER summarized
    away, even if it falls outside the rolling `keep_last` window. This
    is the manual-compression-feedback path: the user types `/pin <N>`
    on turns they care about, and /compact preserves them across the
    summarization boundary. Pinned indexes are auto-remapped after
    compaction so they keep pointing at the right turn.

    Idempotent on a session with ≤ keep_last turns + no pins to preserve.
    """
    n = len(c.turns)
    if n <= keep_last:
        return c

    keep_indexes: set[int] = set(range(n - keep_last, n))
    # Add pinned indexes (within bounds) to the keep set.
    for p in c.pinned_turns:
        if 0 <= p < n:
            keep_indexes.add(p)

    # Anything not in the keep set is OLDER and gets summarized.
    older_idx = [i for i in range(n) if i not in keep_indexes]
    if not older_idx:
        # Only pinned + recent; nothing to compact.
        return c

    older = [c.turns[i] for i in older_idx]
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

    if not new_summary:
        return c

    # Build the new turns list (sorted by original index) + remap pins.
    keep_sorted = sorted(keep_indexes)
    new_turns = [c.turns[i] for i in keep_sorted]
    # Pin indexes were into the OLD list. Translate each to its new
    # position in the keep_sorted list (skip pins that no longer match).
    index_map = {old: new for new, old in enumerate(keep_sorted)}
    new_pins = [index_map[p] for p in c.pinned_turns if p in index_map]

    c.summary = new_summary
    c.turns = new_turns
    c.pinned_turns = new_pins
    c.last_updated = _iso_now()
    return c
