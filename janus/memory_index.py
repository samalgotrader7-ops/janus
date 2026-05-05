"""
memory_index.py — SQLite cache over memory cards (v1.18.0 Phase 2).

CACHE INVARIANT:
The cards/ directory is canonical. index.db is a CACHE — derivable
entirely from the cards. `rm index.db` followed by reconcile() must
produce the same DB contents as before. P5 holds: cards stay plain-text.

RECONCILE ALGORITHM:
Cards are MUTABLE (edits, supersedes). We can't use byte-offset sync
like index.py does for the append-only log.jsonl. Instead:
  - sha256-hash each card file's full text
  - cards_seen tracks (id, sha) — drift detected by sha mismatch
  - phantom check: id in cards_seen but file missing on disk → delete

Reconcile is idempotent and ~O(N): one sha per card on startup. At 5K
cards this is well under 200ms cold on Windows. Recall does NOT require
reconcile to have run — it does its own Path.exists() phantom guard.

WHY NO contentless FTS5:
FTS5 with `content=''` (contentless / external-content) requires
delete-then-insert for updates and complicates iteration. We use a
plain FTS5 table that stores subject + content directly. At 5K cards
× ~200 chars average that's ~1MB of duplicated text — negligible.
"""

from __future__ import annotations
import datetime as _dt
import hashlib
import sqlite3
from pathlib import Path

from . import config, memory_cards


SCHEMA = """
CREATE TABLE IF NOT EXISTS cards_seen (
  id           TEXT PRIMARY KEY,
  path         TEXT NOT NULL,
  mtime        REAL NOT NULL,
  sha          TEXT NOT NULL,
  type         TEXT NOT NULL,
  subject      TEXT NOT NULL,
  confidence   REAL NOT NULL,
  importance   REAL NOT NULL,
  durability   REAL NOT NULL,
  scope        TEXT NOT NULL,
  created      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_type_subject ON cards_seen(type, subject);
CREATE INDEX IF NOT EXISTS idx_scope        ON cards_seen(scope);

CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
  id UNINDEXED,
  type UNINDEXED,
  subject,
  content,
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS stats (
  card_id        TEXT PRIMARY KEY,
  recall_count   INTEGER NOT NULL DEFAULT 0,
  last_recalled  TEXT
);
"""


def _db_path() -> Path:
    return config.MEMORY_DIR / "index.db"


def _connect() -> sqlite3.Connection:
    config.ensure_home()
    config.MEMORY_CARDS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path()))
    conn.executescript(SCHEMA)
    return conn


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------- Reconcile ----------


def reconcile(conn: sqlite3.Connection | None = None) -> dict:
    """Sync the cache to canonical cards/. Returns counts of changes.

    Returns ``{"added": int, "updated": int, "deleted": int, "unchanged": int}``.
    Malformed cards on disk are skipped silently; reconcile must never
    crash on a single bad card or the whole memory subsystem dies.
    """
    own = conn is None
    if own:
        conn = _connect()
    counts = {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    try:
        seen_ids: set[str] = set()
        for path in memory_cards.list_card_paths():
            cid = path.stem
            seen_ids.add(cid)
            try:
                sha = _hash_file(path)
                mtime = path.stat().st_mtime
            except OSError:
                continue
            row = conn.execute(
                "SELECT sha FROM cards_seen WHERE id=?", (cid,)
            ).fetchone()
            if row is None:
                try:
                    card = memory_cards.read_card(path)
                except (memory_cards.CardValidationError, OSError, ValueError):
                    continue
                _insert(conn, card, path, mtime, sha)
                counts["added"] += 1
            elif row[0] != sha:
                try:
                    card = memory_cards.read_card(path)
                except (memory_cards.CardValidationError, OSError, ValueError):
                    continue
                _update(conn, card, path, mtime, sha)
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1

        # Phantom check: in DB but not on disk → drop.
        rows = conn.execute("SELECT id FROM cards_seen").fetchall()
        for (db_id,) in rows:
            if db_id not in seen_ids:
                _delete(conn, db_id)
                counts["deleted"] += 1

        conn.commit()
    finally:
        if own:
            conn.close()
    return counts


def _insert(conn, card, path, mtime, sha) -> None:
    conn.execute(
        "INSERT INTO cards_seen(id, path, mtime, sha, type, subject, "
        "confidence, importance, durability, scope, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (card.id, str(path), mtime, sha, card.type, card.subject,
         card.confidence, card.importance, card.durability, card.scope,
         card.created),
    )
    conn.execute(
        "INSERT INTO cards_fts(id, type, subject, content) "
        "VALUES (?, ?, ?, ?)",
        (card.id, card.type, card.subject, card.content),
    )
    conn.execute(
        "INSERT OR IGNORE INTO stats(card_id) VALUES (?)", (card.id,),
    )


def _update(conn, card, path, mtime, sha) -> None:
    conn.execute(
        "UPDATE cards_seen SET path=?, mtime=?, sha=?, type=?, subject=?, "
        "confidence=?, importance=?, durability=?, scope=?, created=? "
        "WHERE id=?",
        (str(path), mtime, sha, card.type, card.subject,
         card.confidence, card.importance, card.durability, card.scope,
         card.created, card.id),
    )
    conn.execute(
        "UPDATE cards_fts SET type=?, subject=?, content=? WHERE id=?",
        (card.type, card.subject, card.content, card.id),
    )


def _delete(conn, card_id: str) -> None:
    conn.execute("DELETE FROM cards_seen WHERE id=?", (card_id,))
    conn.execute("DELETE FROM cards_fts WHERE id=?", (card_id,))
    conn.execute("DELETE FROM stats WHERE card_id=?", (card_id,))


# ---------- Query ----------


def _escape_fts(query: str) -> str:
    """Tokenize user input for FTS5 — mirrors index._escape_fts."""
    bad = set('"():*^')
    tokens: list[str] = []
    for raw in query.split():
        t = "".join(c for c in raw if c not in bad).strip()
        if not t:
            continue
        tokens.append(f'"{t}"')
    return " OR ".join(tokens) if tokens else ""


def _row_to_dict(cur, row) -> dict:
    return dict(zip([c[0] for c in cur.description], row))


def query_fts(query: str, limit: int = 30) -> list[dict]:
    """FTS5 search. Returns metadata dicts ordered by BM25 (best first).

    The ``score`` field is ``-bm25()`` so higher = better, matching the
    convention in ``index.py``. Recall (Phase 3) reads ``score`` and
    multiplies by recency_decay before final top-K selection.
    """
    fts_query = _escape_fts(query)
    if not fts_query:
        return []
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT s.id, s.type, s.subject, s.confidence, s.importance, "
            "       s.durability, s.scope, s.created, s.path, "
            "       f.content AS content, "
            "       -bm25(cards_fts) AS score "
            "FROM cards_fts f JOIN cards_seen s ON s.id = f.id "
            "WHERE cards_fts MATCH ? "
            "ORDER BY score DESC LIMIT ?",
            (fts_query, limit),
        )
        return [_row_to_dict(cur, row) for row in cur.fetchall()]
    finally:
        conn.close()


def lookup_by_id(card_id: str) -> dict | None:
    """Get one card's metadata. None if not in cache."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT id, type, subject, confidence, importance, durability, "
            "       scope, created, path FROM cards_seen WHERE id=?",
            (card_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None
    finally:
        conn.close()


def lookup_by_subject(type: str, subject: str) -> list[dict]:
    """Find cards with the same (type, subject). For Phase 5 conflict detection.

    Newest first — extractor sees the freshest card first when deciding
    replace vs append vs ignore.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT id, type, subject, confidence, importance, durability, "
            "       scope, created, path FROM cards_seen "
            "WHERE type=? AND subject=? ORDER BY created DESC",
            (type, subject),
        )
        return [_row_to_dict(cur, row) for row in cur.fetchall()]
    finally:
        conn.close()


def list_all(scope: str | None = None, type: str | None = None) -> list[dict]:
    """List all cards, optionally filtered. Newest first."""
    conn = _connect()
    try:
        clauses: list[str] = []
        params: list = []
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if type:
            clauses.append("type=?")
            params.append(type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = conn.execute(
            "SELECT id, type, subject, confidence, importance, durability, "
            f"       scope, created, path FROM cards_seen{where} "
            "ORDER BY created DESC",
            params,
        )
        return [_row_to_dict(cur, row) for row in cur.fetchall()]
    finally:
        conn.close()


# ---------- Stats ----------


def bump_recall(card_ids: list[str], when: str | None = None) -> None:
    """Mark these cards as recalled now. No-op for empty list."""
    if not card_ids:
        return
    when = when or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _connect()
    try:
        for cid in card_ids:
            conn.execute(
                "INSERT INTO stats(card_id, recall_count, last_recalled) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(card_id) DO UPDATE SET "
                "  recall_count = recall_count + 1, "
                "  last_recalled = excluded.last_recalled",
                (cid, when),
            )
        conn.commit()
    finally:
        conn.close()


def get_stats(card_id: str) -> dict | None:
    """Recall stats for a card. None if no row (never recalled or unknown)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT recall_count, last_recalled FROM stats WHERE card_id=?",
            (card_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"recall_count": row[0], "last_recalled": row[1]}
    finally:
        conn.close()


def summary() -> dict:
    """Aggregate stats for /memory stats."""
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM cards_seen").fetchone()[0]
        per_type = dict(
            conn.execute(
                "SELECT type, COUNT(*) FROM cards_seen GROUP BY type"
            ).fetchall()
        )
        per_scope = dict(
            conn.execute(
                "SELECT scope, COUNT(*) FROM cards_seen GROUP BY scope"
            ).fetchall()
        )
        total_recalls = conn.execute(
            "SELECT COALESCE(SUM(recall_count), 0) FROM stats"
        ).fetchone()[0]
        most_recalled = conn.execute(
            "SELECT s.card_id, c.type, c.subject, s.recall_count "
            "FROM stats s JOIN cards_seen c ON c.id = s.card_id "
            "WHERE s.recall_count > 0 "
            "ORDER BY s.recall_count DESC LIMIT 5"
        ).fetchall()
        return {
            "total": total,
            "per_type": per_type,
            "per_scope": per_scope,
            "total_recalls": int(total_recalls or 0),
            "most_recalled": [
                {"id": r[0], "type": r[1], "subject": r[2], "recall_count": r[3]}
                for r in most_recalled
            ],
            "db_path": str(_db_path()),
        }
    finally:
        conn.close()


def reset() -> None:
    """Delete the cache file. Next reconcile() rebuilds from cards/.

    Used by /memory reindex. Caller must invoke reconcile() afterward
    if they want the cache repopulated.
    """
    db = _db_path()
    if db.exists():
        db.unlink()
