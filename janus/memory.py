"""
memory.py — persistent multi-category memory in plain markdown (v1.3).

DESIGN NOTE:
Each memory category is a markdown file the *user owns*. We never silently
rewrite. Every change is proposed as a diff and approved (y/N/edit).

Why markdown, not JSON or SQLite?
  - The user reads it. They edit it directly when they disagree.
  - It's a prompt fragment by construction — no serialization layer to
    debug when the LLM stops respecting it.
  - It diffs cleanly in git.

CATEGORIES (v1.3):
  ~/.janus/memory/soul.md          — agent identity (name, role, tone)
  ~/.janus/memory/user.md          — who the user is
  ~/.janus/memory/project.md       — current workspace / project context
  ~/.janus/memory/preferences.md   — style, format, output preferences
  ~/.janus/memory/relationships.md — other people in user's life

Order matters: earlier categories weigh more. Soul is first so the agent's
identity frames everything else. Users can drop additional .md files in
MEMORY_DIR and they're auto-loaded after the named categories in alpha order.

MIGRATION (one-time, non-destructive):
  ~/.janus/user.md → ~/.janus/memory/user.md
  Triggered automatically on first read; idempotent. If both files exist,
  the new path wins and the legacy file is left alone (warning logged).

PROPOSE-AND-APPROVE FLOW (unchanged):
After each interaction, cli.py calls propose_diff(). Janus emits a list of
{op, category, section, text} ops. CLI shows them; user approves or edits.
Atomic write on approve (write-to-tmp + os.replace).

BACKWARD COMPAT:
Every public function takes an optional `category` argument defaulting to
"user". Pre-v1.3 callers (read(), apply(ops), read_section(name)) keep
working without change.
"""

from __future__ import annotations
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from . import config, llm


# ---------- Public types ----------


class Op(TypedDict, total=False):
    op: str           # "append" | "replace" | "delete" | "create_section"
    section: str      # H2 section name (without leading "## ")
    text: str         # body text (ignored for "delete")
    category: str     # v1.3: which memory file (default "user")


# ---------- Path helpers ----------


def category_path(category: str = "user") -> Path:
    """Path to the .md file for a memory category."""
    return config.MEMORY_DIR / f"{category}.md"


def list_categories() -> list[str]:
    """Every memory category that has a non-empty file, in priority order.

    Order: configured MEMORY_CATEGORIES first (soul, user, project, …),
    then any extras the user has dropped in MEMORY_DIR (alpha-sorted).
    Empty files are omitted — they don't pollute the system prompt.
    """
    _migrate_legacy_user_md()
    out: list[str] = []
    seen: set[str] = set()
    for cat in config.MEMORY_CATEGORIES:
        if read(cat).strip():
            out.append(cat)
            seen.add(cat)
    if config.MEMORY_DIR.is_dir():
        for p in sorted(config.MEMORY_DIR.glob("*.md")):
            cat = p.stem
            if cat in seen or cat.startswith("_") or cat.startswith("."):
                continue
            if read(cat).strip():
                out.append(cat)
                seen.add(cat)
    return out


# ---------- Migration ----------


def _migrate_legacy_user_md() -> bool:
    """Move ~/.janus/user.md → ~/.janus/memory/user.md if needed.

    Idempotent + non-destructive. Called from every read path; cost is
    one stat call when the new path already exists (the steady state).
      - both files exist → leave both alone (new path wins on read; legacy
        becomes a no-op backup the user can delete)
      - only legacy exists → move it to the new path
      - only new exists or neither → no-op
    Returns True only when a move actually happened (for tests).
    """
    target = category_path("user")
    if target.is_file():
        return False
    legacy = config.USER_MODEL_FILE
    if not legacy.is_file():
        return False
    try:
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
        return True
    except OSError:
        return False


# ---------- Read ----------


def read(category: str = "user") -> str:
    """Return the full text of a memory category, or '' if missing.

    Backward-compat: read() with no args returns user.md content.
    """
    _migrate_legacy_user_md()
    p = category_path(category)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def read_section(name: str, category: str = "user") -> str | None:
    """Return body of a section (between its H2 and the next H2/EOF), or None."""
    txt = read(category)
    if not txt:
        return None
    sections = parse_sections(txt)
    return sections.get(name)


def parse_sections(text: str) -> dict[str, str]:
    """Split markdown into {section_name: body}. H1 is preamble (key '')."""
    sections: dict[str, str] = {}
    current = ""  # preamble before first H2
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    sections[current] = "\n".join(buf).strip()
    return sections


def render_sections(sections: dict[str, str]) -> str:
    """Inverse of parse_sections. Preserves preamble-first order if present."""
    parts: list[str] = []
    if "" in sections and sections[""]:
        parts.append(sections[""])
    for name, body in sections.items():
        if name == "":
            continue
        parts.append(f"## {name}")
        if body:
            parts.append(body)
    return "\n\n".join(parts).rstrip() + "\n"


def prepend_for_prompt() -> str:
    """Return the multi-category memory chunk to prepend to system prompts.

    Concatenates every non-empty category in priority order. Each category
    is independently truncated to MEMORY_PREPEND_BYTES so a single overgrown
    file can't crowd out the others.

    v1.7.0: also appends a live state-introspection block (memory_state)
    listing installed agents/swarms/skills + recent fires. The model used
    to grep for these every turn; now it gets the answer up front. Empty
    when nothing's installed yet — keeps the prompt tight on fresh installs.

    Returns '' if NEITHER memory NOR live state has anything to say.
    """
    cats = list_categories()
    n = config.MEMORY_PREPEND_BYTES
    parts: list[str] = []
    for cat in cats:
        body = read(cat).strip()
        if not body:
            continue
        if len(body) > n:
            body = body[:n] + "\n[truncated for prompt]"
        parts.append(f"## from ~/.janus/memory/{cat}.md\n\n{body}")

    # Live introspection — agents, swarms, skill counts, recent fires.
    # Lazy import to avoid a cycle at module load (memory_state reads config).
    state_block = ""
    try:
        from . import memory_state
        state_block = memory_state.state_block().strip()
    except Exception:
        # If introspection fails for any reason, the memory prompt
        # should still work — we just lose the state block.
        state_block = ""

    # v1.15.0 — project instructions (CLAUDE.md / JANUS.md / AGENTS.md
    # auto-loaded from CWD upward). Highest priority, prepended FIRST
    # so project conventions override generic agent advice.
    project_block = ""
    try:
        from . import project_context
        project_block = project_context.load_block().strip()
    except Exception:
        project_block = ""

    if not parts and not state_block and not project_block:
        return ""

    out_parts: list[str] = []
    if project_block:
        out_parts.append(project_block)
    if parts:
        header = "# Memory (persistent state across conversations)"
        out_parts.append(
            header + "\n\n" + "\n\n---\n\n".join(parts)
        )
    if state_block:
        out_parts.append(state_block)
    return "\n\n---\n\n".join(out_parts) + "\n\n---\n"


# ---------- Apply ----------


def apply(ops: list[Op], category: str = "user") -> None:
    """Apply a list of ops atomically, routed by each op's `category` field.

    Backward-compat: ops without a `category` field default to the
    `category` argument (which defaults to "user"). So pre-v1.3 callers
    that pass ops without category keep writing to user.md.
    """
    by_cat: dict[str, list[Op]] = {}
    for op in ops:
        cat = (op.get("category") or category).strip() or "user"
        by_cat.setdefault(cat, []).append(op)
    for cat, cat_ops in by_cat.items():
        _apply_to_category(cat, cat_ops)


def _apply_to_category(category: str, ops: list[Op]) -> None:
    p = category_path(category)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    sections = parse_sections(existing)
    for op in ops:
        kind = op.get("op", "")
        section = (op.get("section") or "").strip()
        text = (op.get("text") or "").strip()
        if not section:
            continue
        if kind == "create_section":
            sections.setdefault(section, text)
        elif kind == "append":
            current = sections.get(section, "")
            sections[section] = (current + "\n" + text).strip() if current else text
        elif kind == "replace":
            sections[section] = text
        elif kind == "delete":
            sections.pop(section, None)
        # silent on unknown ops — propose step is the validation point

    rendered = render_sections(sections)
    if "" not in sections or not sections[""]:
        rendered = f"# {category}.md\n\n" + rendered
    _atomic_write(p, rendered)


def _atomic_write(path: Path, content: str) -> None:
    config.ensure_home()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- Propose (LLM) ----------


PROPOSE_SYSTEM = """You maintain a persistent multi-category memory in plain markdown.

You will receive:
  - the user's request,
  - the agent's final output,
  - the current memory across all categories.

Decide if anything DURABLE was revealed. Durable means it would plausibly
matter again in a future, unrelated conversation.

Route each op to the right category:
  - "soul": the AGENT's identity (name, role, persona, tone). Update this
    when the user names the agent, gives it a personality, or shapes how
    it should behave.
  - "user": who the USER is (identity, role, expertise, long-term interests).
  - "project": current workspace / project context that's actively being
    worked on. Decays — facts here may need replacing later.
  - "preferences": HOW the user wants the agent to communicate (style,
    format, output language, tone, terseness, emoji use).
  - "relationships": other people in the user's life (collaborators,
    family, contacts) IF the user explicitly mentioned them.

Be conservative. Most turns produce ZERO ops. Empty list is the right
answer unless something genuinely new and durable showed up. Do NOT
record one-off facts ("they asked about X today"), session noise, or
content the user could equally get from public docs.

Return STRICT JSON:
{
  "ops": [
    {"op": "append" | "replace" | "create_section" | "delete",
     "category": "soul" | "user" | "project" | "preferences" | "relationships",
     "section": "<H2 name>",
     "text": "<markdown body, or empty for delete>"}
  ]
}

No prose, no markdown fences, no commentary."""


def _parse_ops(data: dict) -> list[Op]:
    """Pull ops out of the LLM JSON response. Defensive; bad ops dropped."""
    valid_cats = set(config.MEMORY_CATEGORIES)
    ops_raw = data.get("ops") or []
    out: list[Op] = []
    for op in ops_raw[:20]:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).strip()
        section = str(op.get("section", "")).strip()
        text = str(op.get("text") or "").strip()
        cat = str(op.get("category") or "user").strip() or "user"
        if cat not in valid_cats and not category_path(cat).exists():
            cat = "user"
        if kind in ("append", "replace", "create_section", "delete") and section:
            out.append({"op": kind, "category": cat, "section": section, "text": text})
    return out


def propose_diff(request: str, output: str) -> dict:
    """Ask the LLM to propose memory updates across all categories.

    v1.18: returns ``{"ops": [...], "cards": [CardProposal]}``. The
    ``cards`` list is empty when the model proposes no typed cards
    (most turns). Single LLM call produces both outputs; cost unchanged
    from the v1.3 ops-only flow.

    Callers that previously did ``ops = memory.propose_diff(req, out)``
    should now do
    ``result = memory.propose_diff(req, out); ops = result["ops"]``.
    """
    if not config.MEMORY_PROPOSE_ENABLED:
        return {"ops": [], "cards": []}
    # v1.18: /memory pause writes a marker file; honor it here.
    if (config.MEMORY_DIR / "_paused").exists():
        return {"ops": [], "cards": []}

    cats_block = []
    for cat in list_categories() or config.MEMORY_CATEGORIES:
        body = read(cat).strip() or "(empty)"
        cats_block.append(f"### memory/{cat}.md\n{body[:2000]}")
    current = "\n\n".join(cats_block) if cats_block else "(empty)"

    # v1.18: append the typed-cards section to the system prompt.
    from . import memory_extract, session_context
    current_scope = session_context.current_scope()
    existing_block = memory_extract.render_existing_cards_block()
    extension = memory_extract.build_extension(
        current_scope=current_scope,
        existing_block=existing_block,
    )

    user_msg = (
        f"User request:\n{request}\n\n"
        f"Agent final output:\n{output[:4000]}\n\n"
        f"Current memory across categories:\n{current[:8000]}"
    )
    msg = _chat_with_model(
        model=config.memory_model(),
        messages=[
            {"role": "system", "content": PROPOSE_SYSTEM + "\n\n" + extension},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        json_mode=True,
    )
    try:
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception:
        return {"ops": [], "cards": []}

    ops = _parse_ops(data)
    cards = memory_extract.parse_cards(
        data, current_scope=current_scope, origin_kind="user_turn",
    )

    # v1.19.0 Phase 7 — heuristic inferred-suggestion scan. Pure compute;
    # no extra LLM call. Queues at most one offer per turn for the
    # NEXT turn's pre-reply prepend.
    try:
        from . import interview_inferred
        origin = session_context.get_origin()
        gateway = origin.get("platform") or "cli"
        chat_id = origin.get("chat_id") or "default"
        interview_inferred.scan_and_queue(
            request, output,
            gateway=gateway, chat_id=str(chat_id),
        )
    except Exception:
        pass

    return {"ops": ops, "cards": cards}


def apply_cards(
    proposals: list,
    *,
    conversation_id: str = "",
    turn: int = 0,
    gateway: str = "",
) -> list[str]:
    """Apply a list of CardProposal objects.

    Handles conflict resolution at apply time:
      - ``ignore`` → skip
      - ``mark_uncertain`` → clamp confidence to 0.5
      - ``replace`` → supersede existing UNLESS any existing card has
        durability >= MEMORY_PROTECTED_DURABILITY (identity-class
        protection); in that case, fall through to append-style write
      - ``append`` → just write the new card

    Provenance (conversation_id, turn, gateway) is filled here from
    the caller's session — extractor never sees those fields.

    Returns the list of card ids actually written. After all writes,
    triggers a single ``memory_index.reconcile()`` so the new cards are
    immediately searchable.
    """
    from . import memory_cards, memory_index, session_context
    if not proposals:
        return []

    # Default gateway from origin if caller didn't supply one.
    if not gateway:
        gateway = session_context.get_origin().get("platform") or "cli"

    written: list[str] = []
    for p in proposals:
        if getattr(p, "conflict_resolution", "append") == "ignore":
            continue

        existing = memory_index.lookup_by_subject(p.type, p.subject)

        confidence = p.confidence
        if p.conflict_resolution == "mark_uncertain":
            confidence = min(confidence, 0.5)

        if p.conflict_resolution == "replace" and existing:
            max_dur = max(float(e["durability"]) for e in existing)
            if max_dur >= config.MEMORY_PROTECTED_DURABILITY:
                # Refuse to supersede identity-class cards. Fall through:
                # the new card is still written; both coexist (effectively
                # append). Recall ranks by recency.
                pass
            else:
                for e in existing:
                    memory_cards.supersede(e["id"])

        source = memory_cards.Source(
            conversation_id=conversation_id,
            turn=turn,
            gateway=gateway,
            origin_kind=getattr(p, "origin_kind", "user_turn"),
        )
        try:
            card = memory_cards.make_card(
                type=p.type,
                subject=p.subject,
                content=p.content,
                confidence=confidence,
                importance=p.importance,
                durability=p.durability,
                scope=p.scope,
                source=source,
            )
            memory_cards.write_card(card)
            written.append(card.id)
        except memory_cards.CardValidationError:
            continue

    if written:
        try:
            memory_index.reconcile()
        except Exception:
            pass

    return written


def _chat_with_model(*, model: str, messages, temperature, json_mode) -> dict:
    """Single-shot chat with explicit model override.

    We can't just call llm.chat() because it always uses config.MODEL. We
    duplicate the four lines rather than pull HTTP into this module.
    """
    import requests
    url = f"{config.API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(url, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def render_diff(ops: list[Op]) -> str:
    """Pretty-print ops for the user to review (category-aware)."""
    if not ops:
        return "(no proposed updates)"
    lines: list[str] = []
    for op in ops:
        cat = op.get("category") or "user"
        head = f"[{op['op']}] {cat}.md ## {op['section']}"
        lines.append(head)
        if op["op"] != "delete":
            for ln in op.get("text", "").splitlines():
                lines.append(f"  {ln}")
        lines.append("")
    return "\n".join(lines).rstrip()
