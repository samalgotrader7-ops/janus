"""
memory_migrate.py — one-shot legacy → cards bootstrap (v1.18.0 Phase 9).

Reads ``~/.janus/memory/{soul,user,project,preferences,relationships}.md``
(the legacy 5 .md categories) and parses each H2 section as one memory
card. New cards land in ``cards/`` with ``source.origin_kind=legacy_migration``
so post-migration extraction can dedupe against them.

Idempotent — marker file ``~/.janus/memory/_legacy_migration_done`` means
"don't run again". Users can manually delete the marker to re-run.

WHY THIS EXISTS:
Without migration, the v1.18 extraction model would re-extract facts
already in the legacy files as new cards (it sees them in the prompt
context). Migration creates the cards once, identifying them via the
legacy_migration origin so:

  1. propose_diff's "EXISTING CARDS" inventory shows the migrated cards
     by (type, subject) → model knows the fact already exists → uses
     conflict_resolution=ignore on duplicates rather than re-extract
  2. ``memory_recall.top_k_block`` surfaces these cards in pre-injection
     recall the same as any other card (replacing the unconditional
     full-file dump for the new flow)

The legacy .md files STAY ON DISK after migration. They remain user-
canonical and continue to be read by ``prepend_for_prompt()``. We just
add a structured-card view on top — both surfaces coexist.
"""

from __future__ import annotations
import datetime as _dt
from pathlib import Path

from . import config, memory, memory_cards, memory_index


CATEGORY_TO_TYPE = {
    "soul": "identity",
    "user": "identity",
    "project": "project",
    "preferences": "preference",
    "relationships": "relationship",
}


def _marker_path() -> Path:
    return config.MEMORY_DIR / "_legacy_migration_done"


def is_done() -> bool:
    return _marker_path().exists()


def _slugify(name: str) -> str:
    """Normalize a section header to a card subject."""
    s = (name or "").strip().lower()
    s = "".join(ch if ch.isalnum() or ch in (" ", "_", "-") else "_" for ch in s)
    s = "_".join(s.split())
    return (s[:50] or "untitled")


def maybe_migrate() -> dict:
    """Run migration if not already done.

    Returns the same shape as ``run_once`` plus a ``skipped`` flag when
    already done (so callers can branch on it without re-running).
    """
    if is_done():
        return {"skipped": True, "migrated": 0, "skipped_empty": 0}
    return run_once()


def run_once() -> dict:
    """Force one migration pass.

    Returns ``{"migrated": int, "skipped_empty": int}``.
    """
    counts = {"migrated": 0, "skipped_empty": 0}

    for cat in config.MEMORY_CATEGORIES:
        cat_path = memory.category_path(cat)
        if not cat_path.exists():
            continue
        try:
            text = cat_path.read_text(encoding="utf-8")
        except OSError:
            continue
        sections = memory.parse_sections(text)
        target_type = CATEGORY_TO_TYPE.get(cat, "identity")

        for section_name, body in sections.items():
            body = (body or "").strip()
            if not body or not section_name:
                counts["skipped_empty"] += 1
                continue
            try:
                source = memory_cards.Source(
                    conversation_id="",
                    turn=0,
                    gateway="cli",
                    origin_kind="legacy_migration",
                )
                card = memory_cards.make_card(
                    type=target_type,
                    subject=_slugify(section_name),
                    content=body,
                    # User-curated content has provable durability.
                    confidence=0.7,
                    importance=0.6,
                    durability=0.7,
                    scope="global",
                    source=source,
                )
                memory_cards.write_card(card)
                counts["migrated"] += 1
            except memory_cards.CardValidationError:
                continue

    # Drop the marker so we don't re-run on the next session.
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _marker_path().write_text(
        f"migration completed at "
        f"{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"cards migrated: {counts['migrated']}\n"
        f"empty sections skipped: {counts['skipped_empty']}\n",
        encoding="utf-8",
    )

    if counts["migrated"]:
        try:
            memory_index.reconcile()
        except Exception:
            pass

    return counts


def reset() -> None:
    """Delete the marker so the next maybe_migrate() runs. Test hook."""
    p = _marker_path()
    if p.exists():
        p.unlink()
