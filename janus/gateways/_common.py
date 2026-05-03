"""_common.py — shared infrastructure for every Janus gateway (v1.3).

Five subsystems, all plain-text on disk so users can `cat` and `git diff`:

  PAIRING   — 8-char codes, owner approves via `janus pair approve …`.
              Replaces (and is backward-compatible with) the legacy
              JANUS_TELEGRAM_CHATS env allowlist.
  HOME      — per-gateway home channel registry. `/sethome` writes here;
              triggers and cron output route here by default.
  SOUL      — load soul.md and personalize the agent's greeting.
              The thing that makes the bot "Samoul" not "Janus".
  SESSIONS  — persistent per-chat session storage so messages survive
              gateway restart. (Pre-v1.3 sessions lived in process memory
              only.)
  INDICATORS — uniform live-progress event stream. Each gateway renders
               events to its native UI (Telegram edit, web SSE, WhatsApp
               reactions). Fires from the executor (memory_update,
               skill_loaded, tool_start, tool_end, thinking).

All state lives under ~/.janus/ so it survives upgrades and is auditable.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import config, memory


# ---------- Constants ----------

# Pairing-code alphabet: 32 unambiguous chars (no 0/O/1/I/L). Matches Hermes.
_CODE_ALPHA = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8
_CODE_TTL_SECONDS = 3600  # 1 hour, matches Hermes


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _safe_id(s: str) -> str:
    """Sanitize a chat_id / phone / session_id for filesystem use."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))[:80] or "anon"


def _atomic_write_json(path: Path, data: Any) -> None:
    """Plain-text JSON write; atomic; parent dir created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


# ---------- PAIRING ----------


@dataclass
class PairingCode:
    code: str
    gateway: str            # "telegram" / "web" / "whatsapp" / …
    chat_id: str            # platform-specific id requesting access
    user_label: str = ""    # display name for the owner to recognize who's asking
    created_at: str = ""

    def expired(self) -> bool:
        try:
            t0 = datetime.datetime.fromisoformat(self.created_at)
        except ValueError:
            return True
        delta = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds()
        return delta > _CODE_TTL_SECONDS


def _pairing_dir() -> Path:
    return config.HOME / "pairing"


def _pending_path() -> Path:
    return _pairing_dir() / "pending.json"


def _approved_path() -> Path:
    return _pairing_dir() / "approved.json"


_pairing_lock = threading.Lock()


def _load_pending() -> list[PairingCode]:
    raw = _read_json(_pending_path(), [])
    return [PairingCode(**r) for r in raw if isinstance(r, dict)]


def _save_pending(items: list[PairingCode]) -> None:
    _atomic_write_json(_pending_path(), [asdict(p) for p in items])


def _load_approved() -> dict[str, list[str]]:
    """Returns {gateway: [chat_id, …]}."""
    raw = _read_json(_approved_path(), {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for gw, ids in raw.items():
        if isinstance(ids, list):
            out[str(gw)] = [str(i) for i in ids]
    return out


def _save_approved(d: dict[str, list[str]]) -> None:
    _atomic_write_json(_approved_path(), d)


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHA) for _ in range(_CODE_LEN))


def request_pairing(gateway: str, chat_id: str, user_label: str = "") -> str:
    """Issue a new pairing code for an unrecognized chat. Returns the code.

    If a pending code already exists for this (gateway, chat_id) and is not
    expired, returns the same code rather than minting a new one — so the
    user can re-trigger /start safely.
    """
    config.ensure_home()
    with _pairing_lock:
        pending = _load_pending()
        # Reuse an unexpired pending code for this chat if present.
        for p in pending:
            if p.gateway == gateway and p.chat_id == str(chat_id) and not p.expired():
                return p.code
        # Drop expired entries while we're here.
        pending = [p for p in pending if not p.expired()]
        new = PairingCode(
            code=generate_code(),
            gateway=gateway,
            chat_id=str(chat_id),
            user_label=user_label or "",
            created_at=_now_iso(),
        )
        pending.append(new)
        _save_pending(pending)
        return new.code


def approve_code(code: str) -> PairingCode | None:
    """Owner CLI calls this. Move (gateway, chat_id) from pending → approved.

    Returns the approved PairingCode (so the CLI can echo the gateway/chat
    that was just authorized), or None if the code is unknown/expired.
    """
    code = code.strip().upper()
    config.ensure_home()
    with _pairing_lock:
        pending = _load_pending()
        match: PairingCode | None = None
        kept: list[PairingCode] = []
        for p in pending:
            if p.code == code and not p.expired() and match is None:
                match = p
            else:
                kept.append(p)
        if match is None:
            return None
        _save_pending(kept)
        approved = _load_approved()
        gw_list = approved.setdefault(match.gateway, [])
        if str(match.chat_id) not in gw_list:
            gw_list.append(str(match.chat_id))
        _save_approved(approved)
        return match


def revoke(gateway: str, chat_id: str) -> bool:
    """Remove (gateway, chat_id) from approved. Returns True if removed."""
    with _pairing_lock:
        approved = _load_approved()
        ids = approved.get(gateway, [])
        cid = str(chat_id)
        if cid not in ids:
            return False
        ids.remove(cid)
        approved[gateway] = ids
        _save_approved(approved)
        return True


def list_pending() -> list[PairingCode]:
    with _pairing_lock:
        return [p for p in _load_pending() if not p.expired()]


def list_approved() -> dict[str, list[str]]:
    with _pairing_lock:
        return _load_approved()


def is_authorized(gateway: str, chat_id: str, *, env_allowlist: str = "") -> bool:
    """True if this (gateway, chat_id) has been paired OR matches the legacy
    env-var allowlist (backward-compat path).

    `env_allowlist` is the comma-separated env value (e.g.,
    JANUS_TELEGRAM_CHATS). We accept it as an arg rather than reading env
    here so each gateway can pass its own var — no implicit coupling.
    """
    cid = str(chat_id)
    approved = _load_approved()
    if cid in approved.get(gateway, []):
        return True
    if env_allowlist:
        for tok in env_allowlist.split(","):
            if tok.strip() == cid:
                return True
    return False


# ---------- HOME CHANNEL ----------


def _home_channels_path() -> Path:
    return config.HOME / "home_channels.json"


def get_home(gateway: str) -> str | None:
    """Returns the chat_id designated as home for this gateway, or None."""
    d = _read_json(_home_channels_path(), {})
    if not isinstance(d, dict):
        return None
    val = d.get(gateway)
    return str(val) if val is not None else None


def set_home(gateway: str, chat_id: str) -> None:
    config.ensure_home()
    d = _read_json(_home_channels_path(), {})
    if not isinstance(d, dict):
        d = {}
    d[gateway] = str(chat_id)
    _atomic_write_json(_home_channels_path(), d)


def clear_home(gateway: str) -> None:
    d = _read_json(_home_channels_path(), {})
    if not isinstance(d, dict):
        return
    d.pop(gateway, None)
    _atomic_write_json(_home_channels_path(), d)


def all_homes() -> dict[str, str]:
    d = _read_json(_home_channels_path(), {})
    return {str(k): str(v) for k, v in (d or {}).items()}


# ---------- SOUL ----------


def load_soul(chat_id: str = "") -> str:
    """Full text of soul.md, or '' if empty.

    v1.3 — per-chat overlay: if `chat_id` is given AND the file
    `~/.janus/memory/soul.<safe_id>.md` exists, return the BASE soul
    plus the overlay content concatenated. Lets one Janus carry
    multiple personas (work vs personal, per family member, etc.)
    without forking the agent.
    """
    base = memory.read("soul")
    if not chat_id:
        return base
    overlay_path = config.MEMORY_DIR / f"soul.{_safe_id(chat_id)}.md"
    if not overlay_path.is_file():
        return base
    overlay = overlay_path.read_text(encoding="utf-8").strip()
    if not overlay:
        return base
    if not base:
        return overlay
    return base + "\n\n## (per-chat overlay)\n\n" + overlay


def _first_line_of_section(body: str, *names: str) -> str:
    sections = memory.parse_sections(body)
    for n in names:
        v = (sections.get(n) or "").strip()
        if v:
            for line in v.splitlines():
                line = line.strip(" -*#")
                if line:
                    return line.split("—")[0].strip().split(":")[0].strip()
    return ""


def agent_name(default: str = "Janus", chat_id: str = "") -> str:
    """Best-effort: extract the agent's name from soul.md (with optional
    per-chat overlay so different chats can know the agent by different
    names — e.g., 'Samoul' on Sam's personal chat, 'Wise' for a work chat).
    """
    name = _first_line_of_section(
        load_soul(chat_id), "Name", "Identity", "name", "identity",
    )
    return name or default


def user_name(default: str = "") -> str:
    """Best-effort: extract the user's name from user.md."""
    return _first_line_of_section(
        memory.read("user"), "Name", "Identity", "name", "identity"
    ) or default


def greeting(user_label: str = "", chat_id: str = "") -> str:
    """First-hello greeting personalized via soul.md + user.md.

    `chat_id` enables per-chat soul overlays (v1.3 L3 #6).
    """
    name = agent_name(chat_id=chat_id)
    user = user_name() or user_label.strip()
    if user:
        return f"Hello {user}! 👋 I'm {name}, your AI assistant."
    return f"Hello! 👋 I'm {name}, your AI assistant."


# ---------- SESSIONS (persistent) ----------


@dataclass
class GatewaySession:
    gateway: str
    chat_id: str
    messages: list[dict] = field(default_factory=list)
    mode: str = ""
    created_at: str = ""
    last_updated: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _sessions_dir(gateway: str) -> Path:
    return config.HOME / "sessions" / gateway


def session_path(gateway: str, chat_id: str) -> Path:
    return _sessions_dir(gateway) / f"{_safe_id(chat_id)}.json"


def load_session(gateway: str, chat_id: str) -> GatewaySession:
    """Return the persisted session, or a fresh empty one."""
    p = session_path(gateway, chat_id)
    raw = _read_json(p, None)
    if isinstance(raw, dict):
        return GatewaySession(
            gateway=str(raw.get("gateway") or gateway),
            chat_id=str(raw.get("chat_id") or chat_id),
            messages=list(raw.get("messages") or []),
            mode=str(raw.get("mode") or ""),
            created_at=str(raw.get("created_at") or _now_iso()),
            last_updated=str(raw.get("last_updated") or _now_iso()),
            extras=dict(raw.get("extras") or {}),
        )
    return GatewaySession(
        gateway=gateway,
        chat_id=str(chat_id),
        created_at=_now_iso(),
        last_updated=_now_iso(),
    )


def save_session(sess: GatewaySession) -> None:
    sess.last_updated = _now_iso()
    _atomic_write_json(session_path(sess.gateway, sess.chat_id), asdict(sess))


def list_sessions(gateway: str = "") -> list[GatewaySession]:
    """All saved sessions for one gateway (or all gateways if empty)."""
    out: list[GatewaySession] = []
    base = config.HOME / "sessions"
    if not base.is_dir():
        return out
    if gateway:
        gws = [base / gateway]
    else:
        gws = [p for p in base.iterdir() if p.is_dir()]
    for gw_dir in gws:
        if not gw_dir.is_dir():
            continue
        for p in sorted(gw_dir.glob("*.json")):
            raw = _read_json(p, None)
            if isinstance(raw, dict):
                out.append(load_session(gw_dir.name, raw.get("chat_id") or p.stem))
    return out


# ---------- CROSS-PLATFORM IDENTITY (v1.3 L3 #3) ----------
#
# An "identity" is a logical user. One human can have many (gateway, chat_id)
# pairs (Sam on Telegram, Sam on WhatsApp, Sam on a web tab) that all map
# to the same identity. Linking them gives one shared memory, one cost
# ledger, one "Sam" across surfaces.


def _identities_path() -> Path:
    return config.HOME / "identities.json"


def _load_identities() -> dict[str, list[list[str]]]:
    """Returns {identity_name: [[gateway, chat_id], ...]}."""
    raw = _read_json(_identities_path(), {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[list[str]]] = {}
    for name, pairs in raw.items():
        if isinstance(pairs, list):
            out[str(name)] = [
                [str(p[0]), str(p[1])] for p in pairs
                if isinstance(p, list) and len(p) == 2
            ]
    return out


def _save_identities(d: dict[str, list[list[str]]]) -> None:
    _atomic_write_json(_identities_path(), d)


def link_identity(identity: str, gateway: str, chat_id: str) -> None:
    """Add (gateway, chat_id) to an identity. Creates identity if new."""
    config.ensure_home()
    d = _load_identities()
    bucket = d.setdefault(identity, [])
    pair = [gateway, str(chat_id)]
    if pair not in bucket:
        bucket.append(pair)
    _save_identities(d)


def unlink_identity(gateway: str, chat_id: str) -> str | None:
    """Remove (gateway, chat_id) from whatever identity contains it.

    Returns the identity name it was unlinked from, or None.
    """
    d = _load_identities()
    pair = [gateway, str(chat_id)]
    target: str | None = None
    for name, pairs in d.items():
        if pair in pairs:
            pairs.remove(pair)
            target = name
            break
    if target:
        _save_identities(d)
    return target


def identity_for(gateway: str, chat_id: str) -> str | None:
    """Reverse-lookup: which identity owns (gateway, chat_id)?

    Returns the identity name, or None when not linked. Useful for
    cost ledger keying and per-identity memory overlays.
    """
    d = _load_identities()
    pair = [gateway, str(chat_id)]
    for name, pairs in d.items():
        if pair in pairs:
            return name
    return None


def list_identities() -> dict[str, list[list[str]]]:
    """Snapshot of every linked identity → its (gateway, chat_id) pairs."""
    return _load_identities()


# ---------- LIVE INDICATORS ----------


@dataclass
class Indicator:
    """A single progress event the executor emits as it runs."""
    kind: str                                        # see INDICATOR_KINDS
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = ""


INDICATOR_KINDS = (
    "thinking",         # model started a turn / new step in the loop
    "skill_loaded",     # a skill auto-attached or was promoted via /skill
    "tool_start",       # tool call beginning (payload: name, args_summary)
    "tool_end",         # tool call completed (payload: name, success, brief)
    "memory_update",    # memory.propose_diff returned non-empty ops
    "fork_offer",       # offered an inline fork-this-conversation button
    "approval_needed",  # surfaced an approval keyboard / button
    "stream_chunk",     # token-stream chunk (payload: text)
    "done",             # turn finished (payload: cost_usd, total_tokens)
)


# Glyph map — gateways can use these or override per their UX. Hermes uses
# 🧠 for memory, 📚 for skills, 🔧 for tools, ▉ as a streaming cursor; we
# match for muscle-memory parity.
INDICATOR_GLYPHS = {
    "thinking":         "⚡",
    "skill_loaded":     "📚",
    "tool_start":       "🔧",
    "tool_end":         "✓",
    "memory_update":    "🧠",
    "fork_offer":       "🔱",
    "approval_needed":  "⚠",
    "stream_chunk":     "▉",
    "done":             "✓",
}


class IndicatorEmitter:
    """Subclass per gateway. Default = no-op (CLI doesn't render these inline).

    The executor accepts an optional emitter parameter; gateways pass their
    own subclass. Calling .emit(Indicator(...)) is the only contract.
    """

    def emit(self, ind: Indicator) -> None:  # noqa: ARG002
        pass

    # Convenience methods so call-sites stay readable.
    def thinking(self, note: str = "") -> None:
        self.emit(Indicator("thinking", {"note": note}, _now_iso()))

    def skill_loaded(self, name: str) -> None:
        self.emit(Indicator("skill_loaded", {"name": name}, _now_iso()))

    def tool_start(self, name: str, args_summary: str = "") -> None:
        self.emit(Indicator(
            "tool_start", {"name": name, "args": args_summary[:200]}, _now_iso(),
        ))

    def tool_end(self, name: str, success: bool, brief: str = "") -> None:
        self.emit(Indicator(
            "tool_end", {"name": name, "success": success, "brief": brief[:200]},
            _now_iso(),
        ))

    def memory_update(self, op_count: int, summary: str = "") -> None:
        self.emit(Indicator(
            "memory_update", {"op_count": op_count, "summary": summary[:200]},
            _now_iso(),
        ))

    def stream_chunk(self, text: str) -> None:
        self.emit(Indicator("stream_chunk", {"text": text}, _now_iso()))

    def done(self, cost_usd: float = 0.0, total_tokens: int = 0) -> None:
        self.emit(Indicator(
            "done", {"cost_usd": cost_usd, "total_tokens": total_tokens},
            _now_iso(),
        ))


class CallbackEmitter(IndicatorEmitter):
    """Convenience: emitter that delegates to a single callable.

    Useful when a gateway just wants `lambda ind: ws.send(json.dumps(ind))`.
    """

    def __init__(self, fn: Callable[[Indicator], None]):
        self._fn = fn

    def emit(self, ind: Indicator) -> None:
        try:
            self._fn(ind)
        except Exception:
            # Indicators are best-effort; never let a render failure break
            # the executor (P8: errors are observations).
            pass
