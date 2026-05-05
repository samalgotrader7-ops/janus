"""
memory_cards.py — Card data model and on-disk layout (v1.18.0 Phase 1).

Cards are individual memory facts stored as markdown files at
~/.janus/memory/cards/<id>.md. Frontmatter holds typed metadata
(type/subject/scores/scope/provenance); the body holds the content
verbatim, so `cat cards/*.md | grep ...` works without parsing yaml.

Card IDs are content-derived: <YYYY-MM-DD>-<sha8(content)>. Two writes
of the same content on the same UTC day collide on disk and are
idempotent — important under concurrent telegram + web + cli writes.

This module is the data layer ONLY. No SQLite, no recall, no extraction.
SQLite cache lands in Phase 2; recall in Phase 3; extraction in Phase 5.

WHY NOT FRONTMATTER-ONLY:
The body duplicates `content` rather than living solely in frontmatter
because (a) multi-line content is awkward in single-line yaml scalars,
(b) `grep` on body is the P5 demo. Body is canonical at parse time —
the parser builds Card.content from the body, not from frontmatter.
"""

from __future__ import annotations
import datetime as _dt
import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, skills as _skills


# 8 typed memory categories (post-tightening — episode and reflection
# are artifacts of consolidation, not types; tracked via origin_kind).
TYPES = (
    "identity",
    "preference",
    "goal",
    "project",
    "habit",
    "decision",
    "constraint",
    "relationship",
)

# Where a card came from. Drives privacy invariants: tool_result MUST
# be scoped to current origin, never global.
ORIGIN_KINDS = (
    "user_turn",
    "tool_result",
    "consolidation",
    "legacy_migration",
)

# durability >= this is identity-class — never auto-superseded by
# conflict resolution, even if a newer card has higher confidence.
PROTECTED_DURABILITY = 0.7


@dataclass
class Source:
    conversation_id: str = ""
    turn: int = 0
    gateway: str = ""
    origin_kind: str = "user_turn"


@dataclass
class Card:
    id: str
    type: str
    subject: str
    content: str
    confidence: float
    importance: float
    durability: float
    scope: str
    created: str
    source: Source = field(default_factory=Source)


# ---------- ID derivation ----------


def card_id(content: str, when: _dt.datetime | None = None) -> str:
    """Derive a card ID from content + creation date.

    Same content on the same UTC day → same ID. This makes concurrent
    writes of the same fact idempotent: same id → same file path → the
    second write is a no-op replace.
    """
    when = when or _dt.datetime.now(_dt.timezone.utc)
    date = when.strftime("%Y-%m-%d")
    sha = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:8]
    return f"{date}-{sha}"


def card_path(card_id_str: str) -> Path:
    return config.MEMORY_CARDS_DIR / f"{card_id_str}.md"


# ---------- Validation ----------


class CardValidationError(ValueError):
    """Raised when a card fails schema validation."""


_SCOPE_PREFIXES = ("telegram:", "web:", "whatsapp:", "project:")
_SCOPE_LITERALS = ("global", "cli")


def _validate_scope(scope: str) -> None:
    if scope in _SCOPE_LITERALS:
        return
    for pfx in _SCOPE_PREFIXES:
        if scope.startswith(pfx):
            tail = scope[len(pfx):].strip()
            if not tail:
                raise CardValidationError(f"empty scope tail: {scope!r}")
            return
    raise CardValidationError(f"invalid scope: {scope!r}")


def _validate_score(name: str, val: float) -> None:
    if not (0.0 <= val <= 1.0):
        raise CardValidationError(f"{name} must be in [0,1], got {val}")


def validate(card: Card) -> None:
    """Raise CardValidationError if the card is malformed."""
    if card.type not in TYPES:
        raise CardValidationError(
            f"invalid type {card.type!r}; must be one of {TYPES}"
        )
    if card.source.origin_kind not in ORIGIN_KINDS:
        raise CardValidationError(
            f"invalid origin_kind {card.source.origin_kind!r}; "
            f"must be one of {ORIGIN_KINDS}"
        )
    _validate_score("confidence", card.confidence)
    _validate_score("importance", card.importance)
    _validate_score("durability", card.durability)
    _validate_scope(card.scope)
    if not card.subject.strip():
        raise CardValidationError("subject is empty")
    if not card.content.strip():
        raise CardValidationError("content is empty")


# ---------- Render ----------


def _quote_string(s: str) -> str:
    """Render a string as a yaml-safe double-quoted scalar.

    Always quotes — defensive against subjects/scopes/etc that contain
    `:`, `#`, or look like booleans/numbers (which the parser would
    coerce). Multi-line strings are rejected; cards keep multi-line
    content in the body, not frontmatter.
    """
    s = "" if s is None else str(s)
    if "\n" in s:
        raise ValueError("multi-line scalar not supported in frontmatter")
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render(card: Card) -> str:
    """Render a Card as a complete markdown file: frontmatter + body."""
    validate(card)
    lines = [
        "---",
        f"id: {_quote_string(card.id)}",
        f"type: {_quote_string(card.type)}",
        f"subject: {_quote_string(card.subject)}",
        f"confidence: {card.confidence}",
        f"importance: {card.importance}",
        f"durability: {card.durability}",
        f"scope: {_quote_string(card.scope)}",
        f"created: {_quote_string(card.created)}",
        "source:",
        f"  conversation_id: {_quote_string(card.source.conversation_id)}",
        f"  turn: {card.source.turn}",
        f"  gateway: {_quote_string(card.source.gateway)}",
        f"  origin_kind: {_quote_string(card.source.origin_kind)}",
        "---",
        "",
        card.content.rstrip(),
        "",
    ]
    return "\n".join(lines)


# ---------- Parse ----------


def _str_field(v) -> str:
    """Coerce a frontmatter value back to a string.

    The yaml subset parser turns bare `true`/`42`/`null` into bool/int/
    None. For string-typed fields we want their literal repr so a
    round-trip yields the same Card.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def parse(text: str) -> Card:
    """Parse a card markdown file into a Card. Body becomes Card.content."""
    fm, body = _skills.parse_frontmatter(text)
    if not fm:
        raise CardValidationError("missing or empty frontmatter")

    src_raw = fm.get("source") or {}
    if not isinstance(src_raw, dict):
        src_raw = {}
    source = Source(
        conversation_id=_str_field(src_raw.get("conversation_id")),
        turn=int(src_raw.get("turn") or 0),
        gateway=_str_field(src_raw.get("gateway")),
        origin_kind=_str_field(src_raw.get("origin_kind")) or "user_turn",
    )
    card = Card(
        id=_str_field(fm.get("id")),
        type=_str_field(fm.get("type")),
        subject=_str_field(fm.get("subject")),
        content=body.strip(),
        confidence=float(fm.get("confidence") or 0.0),
        importance=float(fm.get("importance") or 0.0),
        durability=float(fm.get("durability") or 0.0),
        scope=_str_field(fm.get("scope")) or "global",
        created=_str_field(fm.get("created")),
        source=source,
    )
    validate(card)
    return card


# ---------- Disk I/O ----------


def _atomic_write(path: Path, content: str) -> None:
    """Same atomic-write pattern as memory.py: tempfile + os.replace."""
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


def read_card(path: Path) -> Card:
    """Read a card file from disk. Raises if missing or malformed."""
    return parse(path.read_text(encoding="utf-8"))


def write_card(card: Card) -> Path:
    """Write a card to ~/.janus/memory/cards/<id>.md atomically.

    Idempotent: same content on same UTC day → same id → same path → the
    second write replaces the first with byte-identical content. Safe
    under concurrent gateways writing the same fact.
    """
    p = card_path(card.id)
    _atomic_write(p, render(card))
    return p


def make_card(
    *,
    type: str,
    subject: str,
    content: str,
    confidence: float = 0.5,
    importance: float = 0.5,
    durability: float = 0.5,
    scope: str = "global",
    source: Source | None = None,
    when: _dt.datetime | None = None,
) -> Card:
    """Convenience constructor that derives id and created from content+now.

    The ``scope="global"`` default applies only to programmatic calls;
    extraction (Phase 5) computes scope from the current origin and
    enforces "default = current origin, never auto-promote to global."
    """
    when = when or _dt.datetime.now(_dt.timezone.utc)
    return Card(
        id=card_id(content, when),
        type=type,
        subject=subject,
        content=content,
        confidence=confidence,
        importance=importance,
        durability=durability,
        scope=scope,
        created=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        source=source or Source(),
    )


def list_card_paths() -> list[Path]:
    """List active card files. Excludes _superseded/ and dotfiles."""
    if not config.MEMORY_CARDS_DIR.exists():
        return []
    return sorted(
        p for p in config.MEMORY_CARDS_DIR.glob("*.md")
        if p.is_file() and not p.name.startswith(".")
    )


def supersede(card_id_str: str) -> Path | None:
    """Move a card from cards/ to cards/_superseded/.

    Returns the new path, or None if the source card doesn't exist.
    Used by conflict resolution when the extractor decides a new card
    replaces an existing one. Cards are never deleted by this path —
    Phase 8 pruning handles eventual deletion based on age rules.
    """
    src = card_path(card_id_str)
    if not src.exists():
        return None
    sup_dir = config.MEMORY_CARDS_DIR / "_superseded"
    sup_dir.mkdir(parents=True, exist_ok=True)
    dst = sup_dir / src.name
    os.replace(src, dst)
    return dst
