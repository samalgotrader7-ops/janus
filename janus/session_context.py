"""
session_context.py — per-thread origin tracking (v1.10.0, Tier A item 4).

WHY THIS EXISTS:
A user inside Telegram says "build me an agent that fetches AI news every
4 hours and sends it to telegram". Pre-v1.10 the model had to GUESS the
chat_id (or call session_recent and hope) — and Sam had to type it out.

Hermes solves this with HERMES_SESSION_PLATFORM + HERMES_SESSION_CHAT_ID
env vars set by the gateway around each chat handler. agent_create
reads them and pre-fills delivery targets. Same pattern, ported as a
threading.local so we don't leak between concurrent telegram chats.

API:
  set_origin(platform, chat_id, chat_name=None, user=None)
  get_origin() → dict with the four keys, or {} when no context set
  clear_origin()
  origin_context(...)  → context manager (preferred)

CALLERS:
  - gateways/telegram.py — wraps _run_chat_turn
  - gateways/web.py — wraps the chat handler
  - gateways/whatsapp.py — wraps the message handler
  - tools/agent.py — agent_create reads default deliver_to from origin
  - triggers/runtime.py (future) — pre-fills delivery targets when
    creating triggers programmatically

THREADING:
The CLI is single-threaded so module-level state would also work, but
the gateways run a separate thread per concurrent chat (telegram is
asyncio + asyncio.to_thread for sync executor calls; web spawns a
thread per request). threading.local() keeps each chat's origin
isolated — a Telegram bot serving 5 users at once won't cross-wire
their chat_ids.

P5 (plain-text state): origin lives in memory only — never persisted.
The trigger YAML's deliver_to field IS the persistent record.
"""

from __future__ import annotations
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_LOCAL = threading.local()


def set_origin(
    *,
    platform: str,
    chat_id: str,
    chat_name: str | None = None,
    user: str | None = None,
) -> None:
    """Stash the current chat's identity for this thread."""
    _LOCAL.origin = {
        "platform": str(platform),
        "chat_id": str(chat_id),
        "chat_name": chat_name,
        "user": user,
    }


def get_origin() -> dict[str, Any]:
    """Return the current origin dict, or {} when none was set."""
    return getattr(_LOCAL, "origin", None) or {}


def clear_origin() -> None:
    """Remove origin from this thread. Call on chat-handler exit."""
    if hasattr(_LOCAL, "origin"):
        delattr(_LOCAL, "origin")


def deliver_to_default() -> str:
    """The deliver_to value to use when the model didn't supply one.

    "telegram:<chat_id>" when the caller is inside a Telegram chat,
    "log" everywhere else. Empty string from CLI/headless caller means
    the agent's output only goes to log.jsonl + the cron output archive.
    """
    o = get_origin()
    if o.get("platform") == "telegram" and o.get("chat_id"):
        return f"telegram:{o['chat_id']}"
    return "log"


@contextmanager
def origin_context(
    *,
    platform: str,
    chat_id: str,
    chat_name: str | None = None,
    user: str | None = None,
) -> Iterator[None]:
    """Set origin for the duration of the `with` block; clear on exit.

    Usage (gateway side):
        with origin_context(platform="telegram", chat_id=str(chat_id)):
            await asyncio.to_thread(executor.chat, ...)
    """
    prev = getattr(_LOCAL, "origin", None)
    set_origin(platform=platform, chat_id=chat_id,
               chat_name=chat_name, user=user)
    try:
        yield
    finally:
        if prev is None:
            clear_origin()
        else:
            _LOCAL.origin = prev


# ---------- v1.18: scope helpers for memory cards ----------


def current_project_root() -> Path | None:
    """Resolve the project root containing config.WORKSPACE, or None.

    A directory qualifies as a project root if it contains either a
    ``.git/`` subdirectory OR one of the instruction files recognized
    by ``project_context.py`` (CLAUDE.md / JANUS.md / AGENTS.md / etc.).

    Walk-up stops at the home directory (so user-home isn't treated as
    a project even if it accidentally has a CLAUDE.md).
    """
    from . import config, project_context as _pc
    try:
        cur = Path(config.WORKSPACE).resolve()
    except (OSError, RuntimeError):
        return None
    home = Path.home().resolve()
    while True:
        if (cur / ".git").exists():
            return cur
        for fname in _pc.INSTRUCTION_FILENAMES:
            if (cur / fname).is_file():
                return cur
        parent = cur.parent
        if cur == parent or cur == home:
            return None
        cur = parent


def current_scope() -> str:
    """Resolve the scope string for memory cards in this turn.

    Returns one of:
      - ``telegram:<chat_id>`` / ``web:<chat_id>`` / ``whatsapp:<chat_id>``
        when inside a gateway origin
      - ``project:<absolute_path>`` when at CLI inside a recognized project
      - ``cli`` otherwise

    NEVER returns ``global``. Promotion to global requires user gesture
    (Phase 5 extraction enforces this — model can't auto-promote).
    """
    o = get_origin()
    platform = o.get("platform")
    chat_id = o.get("chat_id")
    if platform and chat_id and platform in ("telegram", "web", "whatsapp"):
        return f"{platform}:{chat_id}"
    root = current_project_root()
    if root is not None:
        return f"project:{root}"
    return "cli"


def scope_matches(card_scope: str, current: str,
                  *, cwd: Path | None = None) -> bool:
    """Does a card with ``scope=card_scope`` apply in the current scope?

    Rules:
      - ``card_scope == 'global'`` → always match
      - exact string match → match (covers ``telegram:X``, ``web:X``,
        ``whatsapp:X``, ``cli``, exact ``project:`` paths)
      - ``card_scope`` starts with ``project:`` AND ``cwd`` is at-or-under
        that path → match (a card scoped to a project is visible from
        any subdirectory of that project, regardless of how the current
        scope was resolved)
      - else → no match

    The optional ``cwd`` parameter exists for testing; in production it
    defaults to the current working directory at call time.
    """
    if card_scope == "global":
        return True
    if card_scope == current:
        return True
    if card_scope.startswith("project:"):
        try:
            card_path = Path(card_scope[len("project:"):]).resolve()
        except (ValueError, OSError):
            return False
        cwd = (cwd or Path.cwd()).resolve()
        return cwd == card_path or card_path in cwd.parents
    return False
