"""
index.py — incremental SQLite FTS5 index over log.jsonl.

WHY:
log.jsonl is the source of truth, but grep is the wrong tool for natural-
language search ("that time I tried to merge two excel files"). FTS5 gives
us ranked search with porter stemming for free, in stdlib SQLite.

DESIGN NOTE:
- The .jsonl is authoritative. The DB is a derived view.
- We track byte offset in `meta('last_offset_bytes')` and only ingest the
  tail. Startup cost is O(new lines), not O(all history).
- Crash safety: write the offset AFTER the COMMIT. Worst case we re-ingest a
  few records — we dedupe by (ts, request) hash on insert.
- Rebuild is `rebuild()`: drops everything and re-reads from byte 0.
"""

from __future__ import annotations
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from . import config


SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS log_fts USING fts5(
  ts UNINDEXED,
  request,
  choice UNINDEXED,
  output,
  tools_used,
  feedback UNINDEXED,
  fingerprint UNINDEXED,
  tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS seen (
  fingerprint TEXT PRIMARY KEY
);
"""


@dataclass
class Hit:
    ts: str
    request: str
    output: str
    tools_used: str
    choice: str
    feedback: str
    score: float


def _connect() -> sqlite3.Connection:
    config.ensure_home()
    conn = sqlite3.connect(str(config.SESSIONS_DB))
    conn.executescript(SCHEMA)
    return conn


def _fingerprint(rec: dict) -> str:
    h = hashlib.sha1()
    h.update((rec.get("ts") or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((rec.get("request") or "").encode("utf-8"))
    return h.hexdigest()


def _record_to_row(rec: dict) -> tuple:
    tools = []
    for s in rec.get("trace", []) or []:
        if s.get("type") == "tool_call":
            tools.append(str(s.get("tool", "")))
    return (
        rec.get("ts") or "",
        rec.get("request") or "",
        str(rec.get("choice") or ""),
        rec.get("output") or "",
        " ".join(tools),
        str(rec.get("feedback") or ""),
        _fingerprint(rec),
    )


def _get_offset(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT value FROM meta WHERE key='last_offset_bytes'")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _set_offset(conn: sqlite3.Connection, offset: int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('last_offset_bytes', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(offset),),
    )


# ---------- Public API ----------


def sync() -> int:
    """Ingest new lines from log.jsonl. Returns rows added."""
    if not config.LOG_FILE.exists():
        return 0
    conn = _connect()
    added = 0
    try:
        offset = _get_offset(conn)
        size = config.LOG_FILE.stat().st_size
        if size < offset:
            # log was truncated/rotated — reindex from scratch.
            offset = 0
            conn.execute("DELETE FROM log_fts")
            conn.execute("DELETE FROM seen")
        with config.LOG_FILE.open("rb") as f:
            f.seek(offset)
            data = f.read()
        end_offset = offset + len(data)

        for line in data.splitlines():
            try:
                rec = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            row = _record_to_row(rec)
            fp = row[-1]
            cur = conn.execute("SELECT 1 FROM seen WHERE fingerprint=?", (fp,))
            if cur.fetchone():
                continue
            conn.execute(
                "INSERT INTO log_fts(ts, request, choice, output, tools_used, feedback, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.execute("INSERT INTO seen(fingerprint) VALUES (?)", (fp,))
            added += 1

        _set_offset(conn, end_offset)
        conn.commit()
    finally:
        conn.close()
    return added


def rebuild() -> int:
    """Drop and re-ingest everything. Returns total rows."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM log_fts")
        conn.execute("DELETE FROM seen")
        conn.execute("DELETE FROM meta WHERE key='last_offset_bytes'")
        conn.commit()
    finally:
        conn.close()
    return sync()


def search(query: str, k: int = 10) -> list[Hit]:
    """FTS5 ranked search across request + output + tools_used."""
    if not query.strip():
        return []
    conn = _connect()
    try:
        # bm25() lower = better; we negate so highest = best.
        cur = conn.execute(
            "SELECT ts, request, output, tools_used, choice, feedback, "
            "       -bm25(log_fts) AS score "
            "FROM log_fts WHERE log_fts MATCH ? ORDER BY score DESC LIMIT ?",
            (_escape_fts(query), k),
        )
        return [Hit(*row) for row in cur.fetchall()]
    finally:
        conn.close()


def search_by_tool(tool_name: str, k: int = 10) -> list[Hit]:
    """Find interactions that used a given tool."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT ts, request, output, tools_used, choice, feedback, 1.0 AS score "
            "FROM log_fts WHERE tools_used MATCH ? ORDER BY ts DESC LIMIT ?",
            (tool_name, k),
        )
        return [Hit(*row) for row in cur.fetchall()]
    finally:
        conn.close()


def recent(k: int = 20) -> list[Hit]:
    """Most-recent N rows, no ranking."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT ts, request, output, tools_used, choice, feedback, 1.0 AS score "
            "FROM log_fts ORDER BY ts DESC LIMIT ?",
            (k,),
        )
        return [Hit(*row) for row in cur.fetchall()]
    finally:
        conn.close()


def stats() -> dict:
    conn = _connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM log_fts").fetchone()[0]
        offset = _get_offset(conn)
        return {
            "rows": n,
            "last_offset_bytes": offset,
            "db_path": str(config.SESSIONS_DB),
            "log_size_bytes": (
                config.LOG_FILE.stat().st_size if config.LOG_FILE.exists() else 0
            ),
        }
    finally:
        conn.close()


# ---------- Helpers ----------


def _escape_fts(query: str) -> str:
    """Wrap FTS query terms to avoid surprises with punctuation in user input.

    For free-text search we tokenize on whitespace, drop FTS5 special chars per
    token, then re-quote each token. This keeps `git pr` matching on either
    word (an OR), which is the user's typical mental model.
    """
    bad = set('"():*^')
    tokens = []
    for raw in query.split():
        t = "".join(c for c in raw if c not in bad).strip()
        if not t:
            continue
        tokens.append(f'"{t}"')
    return " OR ".join(tokens) if tokens else '""'
