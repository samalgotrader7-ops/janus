"""
janus.kanban.store — SQLite-backed task board.

The board is a single sqlite3 file at ~/.janus/kanban.db. All
operations go through this module so the schema is owned in exactly
one place.

Concurrency: every write is wrapped in a transaction. `claim_ready`
uses a `SELECT ... LIMIT 1` + UPDATE inside a transaction with
`BEGIN IMMEDIATE` so two dispatcher threads can race for the same
task without double-claiming.

Schema (v1, may grow over time — use the schema_version row in the
meta table for forward-compatible migrations):

    tasks
      id              INTEGER PRIMARY KEY AUTOINCREMENT
      title           TEXT NOT NULL
      description     TEXT
      status          TEXT NOT NULL   (one of state.ALL_STATES)
      agent_profile   TEXT NOT NULL   (e.g. 'developer', 'researcher')
      workspace       TEXT            (optional cwd for the agent)
      prompt          TEXT            (kicker prompt passed to agent)
      created_at      TEXT NOT NULL   (ISO-8601 UTC)
      claimed_at      TEXT
      completed_at    TEXT
      output          TEXT            (final assistant text on success)
      last_error      TEXT
      retry_count     INTEGER NOT NULL DEFAULT 0
      max_retries     INTEGER NOT NULL DEFAULT 1
      worker_id       TEXT            (which dispatcher worker holds it)

    task_dependencies
      child_id   INTEGER NOT NULL  REFERENCES tasks(id)
      parent_id  INTEGER NOT NULL  REFERENCES tasks(id)
      PRIMARY KEY (child_id, parent_id)

    meta
      key   TEXT PRIMARY KEY
      value TEXT
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator, Optional

from .. import config
from . import state as _state


SCHEMA_VERSION = "1"


# Module-level singleton connection guard — sqlite3 connections aren't
# safe to share across threads unless we serialise access, so we use
# one connection per (thread, db_path) and let SQLite's own locking
# handle inter-thread writes.
_TLS = threading.local()


# ---------- public dataclass ----------


@dataclass
class Task:
    """In-memory representation of one row."""
    id: int = 0
    title: str = ""
    description: str = ""
    status: str = _state.BACKLOG
    agent_profile: str = "developer"
    workspace: str = ""
    prompt: str = ""
    created_at: str = ""
    claimed_at: str = ""
    completed_at: str = ""
    output: str = ""
    last_error: str = ""
    retry_count: int = 0
    max_retries: int = 1
    worker_id: str = ""
    parent_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- connection mgmt ----------


def _db_path() -> Path:
    return Path(config.HOME) / "kanban.db"


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get-or-create a sqlite3 connection for the current thread.

    Each thread holds its own connection (sqlite3's default
    check_same_thread=True). `db_path` lets tests inject an in-memory
    or tempdir DB; production callers omit it.
    """
    path = db_path or _db_path()
    key = f"conn::{path}"
    conn = getattr(_TLS, key, None)
    if conn is not None:
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL = concurrent readers + one writer without blocking, the
    # right mode for a board that's polled by the dispatcher while
    # also queried by `/kanban list`.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    setattr(_TLS, key, conn)
    _ensure_schema(conn)
    return conn


@contextmanager
def _txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Context manager for a single immediate transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables on first connect. Idempotent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT    NOT NULL,
            description   TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL,
            agent_profile TEXT    NOT NULL,
            workspace     TEXT    NOT NULL DEFAULT '',
            prompt        TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL,
            claimed_at    TEXT    NOT NULL DEFAULT '',
            completed_at  TEXT    NOT NULL DEFAULT '',
            output        TEXT    NOT NULL DEFAULT '',
            last_error    TEXT    NOT NULL DEFAULT '',
            retry_count   INTEGER NOT NULL DEFAULT 0,
            max_retries   INTEGER NOT NULL DEFAULT 1,
            worker_id     TEXT    NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status);
        CREATE TABLE IF NOT EXISTS task_dependencies (
            child_id  INTEGER NOT NULL,
            parent_id INTEGER NOT NULL,
            PRIMARY KEY (child_id, parent_id),
            FOREIGN KEY (child_id)  REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_deps_parent
            ON task_dependencies(parent_id);
        CREATE INDEX IF NOT EXISTS idx_deps_child
            ON task_dependencies(child_id);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


# ---------- row <-> Task ----------


def _row_to_task(conn: sqlite3.Connection, row: sqlite3.Row) -> Task:
    parents = [
        r["parent_id"]
        for r in conn.execute(
            "SELECT parent_id FROM task_dependencies WHERE child_id = ?",
            (row["id"],),
        )
    ]
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        agent_profile=row["agent_profile"],
        workspace=row["workspace"],
        prompt=row["prompt"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
        output=row["output"],
        last_error=row["last_error"],
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        worker_id=row["worker_id"],
        parent_ids=parents,
    )


# ---------- public API ----------


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def create_task(
    *,
    title: str,
    agent_profile: str,
    description: str = "",
    workspace: str = "",
    prompt: str = "",
    parent_ids: Optional[list[int]] = None,
    max_retries: int = 1,
    db_path: Optional[Path] = None,
) -> Task:
    """Create a new task. Status starts as BACKLOG if it has parents,
    READY otherwise (so a leaf task is immediately claimable)."""
    parent_ids = list(parent_ids or [])
    conn = _connect(db_path)
    with _txn(conn):
        # Validate parents exist before insert so we don't orphan deps.
        if parent_ids:
            placeholders = ",".join("?" * len(parent_ids))
            existing = {
                r["id"] for r in conn.execute(
                    f"SELECT id FROM tasks WHERE id IN ({placeholders})",
                    parent_ids,
                )
            }
            missing = [p for p in parent_ids if p not in existing]
            if missing:
                raise ValueError(f"parent_ids not found: {missing}")
        # If any parent isn't completed yet, start in BACKLOG.
        # Otherwise READY immediately.
        initial = _state.READY
        if parent_ids:
            unfinished = conn.execute(
                f"SELECT COUNT(*) FROM tasks "
                f"WHERE id IN ({','.join('?' * len(parent_ids))}) "
                f"AND status != ?",
                parent_ids + [_state.COMPLETED],
            ).fetchone()[0]
            if unfinished:
                initial = _state.BACKLOG
        cursor = conn.execute(
            """
            INSERT INTO tasks
              (title, description, status, agent_profile, workspace,
               prompt, created_at, max_retries)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, description, initial, agent_profile, workspace,
             prompt, _now(), max_retries),
        )
        new_id = cursor.lastrowid
        for p in parent_ids:
            conn.execute(
                "INSERT INTO task_dependencies(child_id, parent_id) VALUES (?, ?)",
                (new_id, p),
            )
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (new_id,),
        ).fetchone()
        return _row_to_task(conn, row)


def get_task(task_id: int, *, db_path: Optional[Path] = None) -> Optional[Task]:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return _row_to_task(conn, row) if row else None


def list_tasks(
    *,
    status: Optional[str] = None,
    agent_profile: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[Task]:
    conn = _connect(db_path)
    sql = "SELECT * FROM tasks"
    conds: list[str] = []
    args: list[Any] = []
    if status:
        conds.append("status = ?")
        args.append(status)
    if agent_profile:
        conds.append("agent_profile = ?")
        args.append(agent_profile)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id ASC"
    return [_row_to_task(conn, r) for r in conn.execute(sql, args)]


def set_status(
    task_id: int,
    new_status: str,
    *,
    output: str = "",
    last_error: str = "",
    worker_id: str = "",
    db_path: Optional[Path] = None,
) -> Task:
    """Transition a task to a new status. Validates the transition.

    Returns the updated Task. Raises ValueError on illegal transitions
    or unknown id.

    Side effect: when transitioning to COMPLETED, calls
    advance_dependents() to flip eligible children from BACKLOG to
    READY.
    """
    if new_status not in _state.ALL_STATES:
        raise ValueError(f"unknown status: {new_status!r}")
    conn = _connect(db_path)
    with _txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"task {task_id} not found")
        src = row["status"]
        if not _state.is_legal(src, new_status):
            raise ValueError(
                f"illegal transition for task {task_id}: "
                f"{src!r} -> {new_status!r}"
            )
        now = _now()
        updates = ["status = ?"]
        args: list[Any] = [new_status]
        if new_status == _state.IN_PROGRESS:
            updates.append("claimed_at = ?")
            args.append(now)
            if worker_id:
                updates.append("worker_id = ?")
                args.append(worker_id)
        if new_status in (_state.COMPLETED, _state.FAILED):
            updates.append("completed_at = ?")
            args.append(now)
        if output:
            updates.append("output = ?")
            args.append(output)
        if last_error:
            updates.append("last_error = ?")
            args.append(last_error)
        args.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
            args,
        )
        if new_status == _state.COMPLETED:
            _advance_dependents_locked(conn, task_id)
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        return _row_to_task(conn, row)


def claim_ready(
    *, worker_id: str, db_path: Optional[Path] = None,
) -> Optional[Task]:
    """Atomically pick one READY task and flip it to IN_PROGRESS.

    Returns the claimed Task or None if nothing is ready. Safe for
    concurrent dispatchers — the BEGIN IMMEDIATE transaction serialises
    the select-and-update.
    """
    conn = _connect(db_path)
    with _txn(conn):
        row = conn.execute(
            "SELECT id FROM tasks WHERE status = ? "
            "ORDER BY id ASC LIMIT 1",
            (_state.READY,),
        ).fetchone()
        if row is None:
            return None
        task_id = row["id"]
        conn.execute(
            "UPDATE tasks "
            "SET status = ?, claimed_at = ?, worker_id = ? "
            "WHERE id = ? AND status = ?",
            (_state.IN_PROGRESS, _now(), worker_id, task_id, _state.READY),
        )
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        return _row_to_task(conn, row)


def advance_dependents(
    parent_id: int, *, db_path: Optional[Path] = None,
) -> list[int]:
    """Public wrapper for the dependency-advance step.

    Used by external callers (e.g. tests) who completed a task without
    going through set_status (rare). Returns IDs that became READY.
    """
    conn = _connect(db_path)
    with _txn(conn):
        return _advance_dependents_locked(conn, parent_id)


def _advance_dependents_locked(
    conn: sqlite3.Connection, parent_id: int,
) -> list[int]:
    """Flip BACKLOG children of `parent_id` to READY if all their
    parents are now COMPLETED. Caller holds the write lock."""
    promoted: list[int] = []
    children = [
        r["child_id"]
        for r in conn.execute(
            "SELECT child_id FROM task_dependencies WHERE parent_id = ?",
            (parent_id,),
        )
    ]
    for cid in children:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (cid,),
        ).fetchone()
        if not row or row["status"] != _state.BACKLOG:
            continue
        unfinished = conn.execute(
            "SELECT COUNT(*) FROM task_dependencies td "
            "JOIN tasks t ON t.id = td.parent_id "
            "WHERE td.child_id = ? AND t.status != ?",
            (cid, _state.COMPLETED),
        ).fetchone()[0]
        if unfinished == 0:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (_state.READY, cid),
            )
            promoted.append(cid)
    return promoted


def delete_task(
    task_id: int, *, db_path: Optional[Path] = None,
) -> bool:
    """Delete a task and its dependency rows (CASCADE). Returns True
    if a row was deleted."""
    conn = _connect(db_path)
    with _txn(conn):
        cursor = conn.execute(
            "DELETE FROM tasks WHERE id = ?", (task_id,),
        )
        return cursor.rowcount > 0


def counts_by_status(
    *, db_path: Optional[Path] = None,
) -> dict[str, int]:
    """Return {status: count} for all live statuses (handy for `/kanban status`)."""
    conn = _connect(db_path)
    out = {s: 0 for s in _state.ALL_STATES}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"):
        out[r["status"]] = r["n"]
    return out


def reset_db(*, db_path: Optional[Path] = None) -> None:
    """Wipe everything. For tests only — never called from production."""
    conn = _connect(db_path)
    with _txn(conn):
        conn.execute("DELETE FROM task_dependencies")
        conn.execute("DELETE FROM tasks")
