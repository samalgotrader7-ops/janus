"""
gateways/telegram.py — v1.3 Telegram gateway with pairing, indicators,
self-intro, persistent sessions, and 4-button approval keyboards.

ARCHITECTURE:
Telegram is a chat surface for executor.chat(). Per-chat sessions persist
to ~/.janus/sessions/telegram/<chat_id>.json (v1.3) so messages survive
gateway restart. The first turn from a recognized chat triggers a
self-introduction loaded from soul.md + user.md (gw._common).

ACCESS CONTROL (v1.3):
Unrecognized chats receive an 8-char pairing code and instructions to
ask the bot owner to run `janus pair approve <CODE>`. The legacy
JANUS_TELEGRAM_CHATS env allowlist still works as a fallback.

LIVE INDICATORS:
The executor's on_step callback maps to short Telegram messages with
glyphs (🧠 memory / 📚 skill / 🔧 tool / ⚡ thinking / ✓ done). This
gives Hermes-style engagement without complex editMessageText streaming.

APPROVAL UX (v1.3):
4-button inline keyboard: ✓ Once · ✓ Session · ✓ Always · ✗ Deny.
Session grants are remembered for the rest of this conversation;
Always grants persist to the session file so they survive restart.

USE:
  python -m janus telegram
"""

from __future__ import annotations
import asyncio
import time
import uuid
from typing import Any

from .. import config, executor, logger, memory, index, skills, permissions
from .. import branding, cost
from ..tools import default_registry, make_protected, CapabilitySet
from . import _common as gw


try:
    from telegram import (
        Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
    )
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, CallbackQueryHandler,
        ContextTypes, filters,
    )
    HAVE_TG = True
except ImportError:  # pragma: no cover
    HAVE_TG = False


# Commands surfaced in Telegram's `/` autocomplete menu via setMyCommands.
# Without this call, the commands STILL work when typed, but Telegram's
# UI won't suggest them — which is what made v1.3 feel like the new
# commands "didn't exist" even after the upgrade.
_BOT_COMMANDS = [
    ("start",   "introduce the bot + show current mode and commands"),
    ("mode",    "show or switch permission mode (default/acceptEdits/plan/bypassPermissions)"),
    ("sethome", "set this chat as the home channel for cron/cross-platform"),
    ("skills",  "list installed skills with state and trust score"),
    ("memory",  "show all memory categories, or /memory <cat> for one"),
    ("cost",    "per-chat cost ledger"),
    ("search",  "search prior interactions in the log index"),
    ("swarm",   "agent swarms — list | describe | run | status | cancel"),
    ("clear",   "reset this chat's conversation"),
    ("logo",    "print the bifurcation logo"),
]


MAX_MSG = 3500  # leave headroom under Telegram's 4096
GATEWAY_NAME = "telegram"


# ---------- Per-chat session ----------


class Session:
    """Per-chat conversation state (v1.3 persistent).

    Wraps gw.GatewaySession (which persists to ~/.janus/sessions/telegram/)
    with the per-turn ephemeral pieces: in-memory mode_state, approval
    futures, and session-scoped capability grants.
    """
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self._persistent = gw.load_session(GATEWAY_NAME, str(chat_id))
        # Restore mode from persistent state, else env default.
        mode = self._persistent.mode or permissions.normalize(config.APPROVAL_MODE)
        self.mode_state = permissions.ModeState(current=mode)
        self.approval_futures: dict[str, asyncio.Future] = {}
        # Session-scoped grants — cleared on /clear or process restart.
        # Keyed by "tool.verb" → True.
        self.session_grants: set[str] = set()
        # Always-grants — persisted to session file so they survive restart.
        # Stored under extras["always_grants"].
        self.always_grants: set[str] = set(
            self._persistent.extras.get("always_grants") or []
        )

    @property
    def messages(self) -> list[dict]:
        return self._persistent.messages

    @property
    def greeted(self) -> bool:
        return bool(self._persistent.extras.get("greeted"))

    def mark_greeted(self) -> None:
        self._persistent.extras["greeted"] = True
        self.save()

    def grant_always(self, key: str) -> None:
        self.always_grants.add(key)
        self._persistent.extras["always_grants"] = sorted(self.always_grants)
        self.save()

    def save(self) -> None:
        self._persistent.mode = self.mode_state.current
        gw.save_session(self._persistent)

    def clear(self) -> None:
        self._persistent.messages = []
        self.session_grants.clear()
        self.save()


SESSIONS: dict[int, Session] = {}


def _session(chat_id: int) -> Session:
    s = SESSIONS.get(chat_id)
    if s is None:
        s = Session(chat_id)
        SESSIONS[chat_id] = s
    return s


def _user_label(update: Update) -> str:
    u = update.effective_user
    if not u:
        return ""
    return (u.username or u.full_name or "").strip()


def _is_authorized(chat_id: int) -> bool:
    return gw.is_authorized(
        GATEWAY_NAME, str(chat_id),
        env_allowlist=config.TELEGRAM_ALLOWED_CHATS or "",
    )


# ---------- Output chunking ----------


def _chunks(text: str, n: int = MAX_MSG):
    text = text or ""
    while text:
        if len(text) <= n:
            yield text
            return
        cut = text.rfind("\n", 0, n)
        if cut < 200:
            cut = n
        yield text[:cut]
        text = text[cut:].lstrip()


async def _send(bot, chat_id: int, text: str) -> None:
    for chunk in _chunks(text):
        await bot.send_message(chat_id=chat_id, text=chunk)


# ---------- Mode-aware approver (4-button keyboard) ----------


def _make_approver(chat_id: int, app, sess: Session):
    """v1.3 approver with session+always grants and 4-button keyboard.

    ALLOW   → True silently (mode allows the risk class).
    DENY    → False (the model sees the refusal observation).
    ASK     → check session/always grants; else send 4-button keyboard.
    """
    def approver(action_label: str, details: str, **kw) -> bool:
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        cap = kw.get("capability") or (None, "", None)
        cap_key = f"{cap[0]}.{cap[1]}" if cap[0] else action_label

        decision = permissions.decide(risk, sess.mode_state.current)
        if decision == permissions.ALLOW:
            return True
        if decision == permissions.DENY:
            return False

        # ASK — check pre-existing grants.
        if cap_key in sess.always_grants or cap_key in sess.session_grants:
            return True

        # Send 4-button keyboard.
        loop = asyncio.get_event_loop()
        token = uuid.uuid4().hex[:8]
        fut: asyncio.Future = loop.create_future()
        # Stash key alongside future so the callback knows what to grant.
        sess.approval_futures[token] = fut
        sess.approval_futures[token + ".key"] = cap_key  # type: ignore

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✓ Once", callback_data=f"appr:{token}:once"),
                InlineKeyboardButton("✓ Session", callback_data=f"appr:{token}:sess"),
            ],
            [
                InlineKeyboardButton("✓ Always", callback_data=f"appr:{token}:always"),
                InlineKeyboardButton("✗ Deny", callback_data=f"appr:{token}:deny"),
            ],
        ])
        body = (
            f"⚠ approval needed (risk={risk}, mode={sess.mode_state.current})\n"
            f"*{action_label}*\n\n{details[:1000]}"
        )
        coro = app.bot.send_message(
            chat_id=chat_id, text=body,
            parse_mode="Markdown", reply_markup=kb,
        )
        asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return loop.run_until_complete(asyncio.wait_for(fut, timeout=180))
        except asyncio.TimeoutError:
            return False
    return approver


# ---------- Live indicator emitter (Telegram-flavored) ----------


def _make_telegram_emitter(chat_id: int, app, loop) -> "TelegramEmitter":
    return TelegramEmitter(chat_id, app, loop)


class TelegramEmitter(gw.IndicatorEmitter):
    """Render executor on_step events as short Telegram messages.

    Hermes-style glyphs (🧠 memory / 📚 skill / 🔧 tool / ⚡ thinking).
    Send is fire-and-forget — we don't block the executor on Telegram I/O.
    """

    def __init__(self, chat_id: int, app, loop):
        self.chat_id = chat_id
        self.app = app
        self.loop = loop

    def _send(self, text: str) -> None:
        coro = self.app.bot.send_message(chat_id=self.chat_id, text=text)
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception:
            pass

    def emit(self, ind: gw.Indicator) -> None:
        glyph = gw.INDICATOR_GLYPHS.get(ind.kind, "")
        if ind.kind == "skill_loaded":
            self._send(f"{glyph} skill: {ind.payload.get('name', '?')}")
        elif ind.kind == "memory_update":
            n = ind.payload.get("op_count", 0)
            summary = ind.payload.get("summary") or ""
            self._send(f"{glyph} memory: {n} update(s) proposed{(' — ' + summary[:120]) if summary else ''}")
        elif ind.kind == "tool_start":
            name = ind.payload.get("name", "?")
            args = ind.payload.get("args") or ""
            self._send(f"{glyph} tool: {name}{(' ' + args[:120]) if args else ''}")
        elif ind.kind == "tool_end":
            name = ind.payload.get("name", "?")
            ok = ind.payload.get("success", True)
            self._send(f"{('✓' if ok else '✗')} {name}")
        elif ind.kind == "thinking":
            note = ind.payload.get("note") or ""
            if note:
                self._send(f"{glyph} {note[:120]}")
        # done / stream_chunk are no-ops in this MVP — reserved for richer
        # editMessageText streaming in a follow-up.


def _make_on_step(emitter: TelegramEmitter):
    """Adapt executor.on_step records → IndicatorEmitter calls."""
    def on_step(record: dict):
        t = record.get("type")
        if t == "tool_call":
            args = record.get("args") or {}
            args_summary = ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())
            emitter.tool_start(record.get("tool", "?"), args_summary)
        elif t == "tool_result":
            preview = record.get("result_preview") or ""
            success = "error" not in (preview or "").lower()[:50]
            emitter.tool_end(
                record.get("tool", "?"), success,
                preview[:120] if preview else "",
            )
        # final and stream_chunk: no-op in MVP
    return on_step


# ---------- Handlers ----------


def _logo_block() -> str:
    body = "\n".join(branding.LOGO_LINES)
    return (
        f"```\n{body}\n```\n"
        f"*janus*  v{branding.VERSION}\n"
        f"_{branding.TAGLINE}_"
    )


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update)
        return
    sess = _session(chat_id)
    home = gw.get_home(GATEWAY_NAME)
    home_line = (
        f"home channel: {home}" if home
        else "no home channel set — type /sethome to make this it"
    )
    body = (
        f"{_logo_block()}\n\n"
        f"telegram gateway online.\n"
        f"send any text → chat with the agent.\n\n"
        f"*current mode:* {sess.mode_state.current}\n"
        f"*{home_line}*\n\n"
        "*commands*\n"
        "/mode /sethome /skills /memory /search /logo /eval /clear"
    )
    await update.message.reply_text(body, parse_mode="Markdown")


async def _send_pairing_prompt(update: Update) -> None:
    """Issue a pairing code and tell the user how to get authorized."""
    chat_id = update.effective_chat.id
    user_label = _user_label(update)
    code = gw.request_pairing(GATEWAY_NAME, str(chat_id), user_label)
    label_part = f" ({user_label})" if user_label else ""
    msg = (
        f"Hi! I don't recognize this chat yet.\n\n"
        f"Pairing code: `{code}`\n\n"
        f"Ask the bot owner to run:\n"
        f"`janus pair approve {code}`\n\n"
        f"Once approved, send any message and I'll respond.\n"
        f"_chat id: {chat_id}{label_part}_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_logo(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    await update.message.reply_text(_logo_block(), parse_mode="Markdown")


async def cmd_sethome(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """v1.3: designate this chat as the gateway's home channel.

    Cron output and cross-platform messages route here by default.
    """
    chat_id = update.effective_chat.id
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update)
        return
    gw.set_home(GATEWAY_NAME, str(chat_id))
    label = _user_label(update) or "this chat"
    await update.message.reply_text(
        f"✅ Home channel set to {label} (ID: {chat_id}).\n"
        f"Cron jobs and cross-platform messages will be delivered here.",
    )
    logger.write({
        "ts": logger.now_iso(), "type": "sethome",
        "gateway": GATEWAY_NAME, "chat_id": chat_id,
    })


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update); return
    sess = _session(chat_id)
    args = ctx.args or []
    if not args:
        rows = [
            (permissions.DEFAULT, "read auto · write/exec ask"),
            (permissions.ACCEPT_EDITS, "read+write auto · exec ask"),
            (permissions.PLAN, "read auto · write/exec DENY"),
            (permissions.BYPASS, "everything auto · no prompts"),
        ]
        body = [f"*current mode:* `{sess.mode_state.current}`", ""]
        for name, desc in rows:
            marker = "● " if name == sess.mode_state.current else "  "
            body.append(f"`{marker}{name:<18}` {desc}")
        body.append("")
        body.append("usage: `/mode <name>`")
        await update.message.reply_text("\n".join(body), parse_mode="Markdown")
        return
    target = args[0]
    normalized = permissions.normalize(target)
    if (
        normalized == permissions.DEFAULT
        and target.lower() not in ("manual", "default")
        and target not in permissions.ALL_MODES
    ):
        await update.message.reply_text(
            f"unknown mode: {target}\n"
            f"valid: {', '.join(permissions.ALL_MODES)}"
        )
        return
    sess.mode_state.set(normalized)
    sess.save()
    msg = f"mode → *{sess.mode_state.current}*"
    if normalized == permissions.BYPASS:
        msg += "\n\n⚠ every tool will run without asking."
    await update.message.reply_text(msg, parse_mode="Markdown")
    logger.write({
        "ts": logger.now_iso(), "type": "mode_switch",
        "gateway": GATEWAY_NAME, "chat_id": chat_id, "new_mode": normalized,
    })


async def cmd_swarm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """v1.4: /swarm — agent swarm operations.

    Delegates to swarms.slash.handle so cli_rich, cli, and gateways all
    share the same dispatch logic. The arg string is everything after
    the command name."""
    if not _is_authorized(update.effective_chat.id):
        await _send_pairing_prompt(update); return
    from .. import swarms as _swarms
    arg = " ".join(ctx.args or [])
    text = _swarms.slash.handle(arg)
    await _send(update.get_bot(), update.effective_chat.id, text)


async def cmd_skills(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        await _send_pairing_prompt(update); return
    items = skills.list_skills()
    if not items:
        await update.message.reply_text(
            "no skills yet. run `/skills install-bundled` from the CLI to add 58.")
        return
    lines = [f"• {s.name} ({s.state}) — {s.description}" for s in items[:60]]
    if len(items) > 60:
        lines.append(f"... ({len(items) - 60} more)")
    await _send(update.get_bot(), update.effective_chat.id, "\n".join(lines))


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """v1.3: multi-category. /memory shows all; /memory <cat> shows one."""
    if not _is_authorized(update.effective_chat.id):
        await _send_pairing_prompt(update); return
    arg = " ".join(ctx.args or []).strip()
    if arg:
        body = memory.read(arg)
        if not body.strip():
            await update.message.reply_text(f"(no {arg}.md yet)")
            return
        await _send(update.get_bot(), update.effective_chat.id, body)
        return
    cats = memory.list_categories()
    if not cats:
        await update.message.reply_text(
            "(no memory yet — categories ready: "
            f"{', '.join(config.MEMORY_CATEGORIES)})"
        )
        return
    parts = []
    for cat in cats:
        body = memory.read(cat).strip()
        parts.append(f"━ {cat}.md ({len(body)} bytes) ━\n{body}")
    await _send(update.get_bot(), update.effective_chat.id, "\n\n".join(parts))


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        await _send_pairing_prompt(update); return
    q = " ".join(ctx.args or [])
    if not q:
        await update.message.reply_text("usage: /search <query>")
        return
    index.sync()
    hits = index.search(q, k=5)
    if not hits:
        await update.message.reply_text("no matches.")
        return
    lines = [f"{h.ts[:19]} — {h.request[:80]}" for h in hits]
    await update.message.reply_text("\n".join(lines))


async def cmd_clear(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        await _send_pairing_prompt(update); return
    sess = _session(update.effective_chat.id)
    sess.clear()
    await update.message.reply_text("conversation cleared.")


async def cmd_cost(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """v1.3: per-chat cost summary."""
    chat_id = update.effective_chat.id
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update); return
    identity = gw.identity_for(GATEWAY_NAME, str(chat_id)) or ""
    body = cost.render_per_chat(GATEWAY_NAME, str(chat_id), identity)
    await update.message.reply_text(body)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """v1.3 chat handler with pairing, self-intro, indicators, persistence."""
    chat_id = update.effective_chat.id

    # Unauthorized → pairing flow.
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update)
        return

    sess = _session(chat_id)
    req = update.message.text or ""
    if not req.strip():
        return

    # Self-introduction on first authorized text.
    if not sess.greeted:
        user_label = _user_label(update)
        # Update user.md with the user's display name if we don't have one.
        if user_label and not gw.user_name():
            try:
                memory.apply([{
                    "op": "create_section", "category": "user",
                    "section": "Name", "text": user_label,
                }])
            except Exception:
                pass
        await update.message.reply_text(gw.greeting(user_label))
        sess.mark_greeted()
        # If they said "hi"/"hello", the greeting is the whole reply.
        if req.lower().strip(" .!,?") in ("hi", "hello", "hey", "yo", "sup"):
            return

    await _run_chat_turn(update, ctx, chat_id, sess, req)


# v1.5.1: photo/document upload handlers. Without these, attachments
# silently never reach any callback (the bot didn't even know the user
# uploaded anything). Now uploads land in ~/.janus/uploads/<chat_id>/
# and the path is injected into the conversation as a synthetic user
# message — the system prompt tells the model to call image_describe
# or fs_read on it.


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User uploaded a photo. Download, ack, inject path as a chat turn."""
    chat_id = update.effective_chat.id
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update); return
    sess = _session(chat_id)
    if not update.message.photo:
        return

    # Download highest-resolution variant.
    photo = update.message.photo[-1]
    upload_dir = config.HOME / "uploads" / str(chat_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    local_path = upload_dir / f"photo_{int(time.time())}.jpg"
    try:
        f = await photo.get_file()
        await f.download_to_drive(custom_path=str(local_path))
    except Exception as e:
        await update.message.reply_text(f"failed to download image: {e}")
        return

    await update.message.reply_text(
        f"📷 received image · saved to `{local_path.name}` · processing…",
        parse_mode="Markdown",
    )

    caption = (update.message.caption or "").strip()
    if caption:
        req = f"{caption}\n[user uploaded image at {local_path}]"
    else:
        req = f"[user uploaded image at {local_path}]"

    await _run_chat_turn(update, ctx, chat_id, sess, req)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User uploaded a file. Download, ack, inject path as a chat turn."""
    chat_id = update.effective_chat.id
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update); return
    sess = _session(chat_id)
    doc = update.message.document
    if not doc:
        return

    # Sanitize filename — no path traversal, no shell metachars.
    raw_name = doc.file_name or f"upload_{int(time.time())}"
    safe_name = "".join(
        c if c.isalnum() or c in ".-_" else "_" for c in raw_name
    )[:120]
    upload_dir = config.HOME / "uploads" / str(chat_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    local_path = upload_dir / safe_name
    try:
        f = await doc.get_file()
        await f.download_to_drive(custom_path=str(local_path))
    except Exception as e:
        await update.message.reply_text(f"failed to download file: {e}")
        return

    await update.message.reply_text(
        f"📎 received file · saved to `{safe_name}` ({doc.file_size or '?'} bytes) · processing…",
        parse_mode="Markdown",
    )

    caption = (update.message.caption or "").strip()
    if caption:
        req = f"{caption}\n[user uploaded file at {local_path}]"
    else:
        req = f"[user uploaded file at {local_path}]"

    await _run_chat_turn(update, ctx, chat_id, sess, req)


async def _typing_pulse(bot, chat_id: int, interval_s: float = 4.0) -> None:
    """v1.5.1: Telegram chat_action='typing' lasts ~5s. Pulse every
    `interval_s` (default 4s) so the user sees continuous "typing…"
    dots during long operations. Started at chat-turn start, cancelled
    when the response is sent.

    Cancellation-safe: the asyncio.CancelledError loop exit is the
    intended termination path."""
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                # Network blip / API hiccup — pulse is best-effort.
                pass
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        return


async def _run_chat_turn(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int, sess: "Session", req: str,
):
    """Shared chat-flow used by on_text / on_photo / on_document.
    Runs executor.chat with full memory + indicators + cost tracking."""
    preamble = memory.prepend_for_prompt()

    base_approver = _make_approver(chat_id, ctx.application, sess)
    caps = CapabilitySet()
    tools = default_registry(capabilities=caps)
    # v1.5.1: register the gateway send-file tool so the model can deliver
    # files as attachments (not paste content). Closure captures the bot
    # + chat_id + asyncio loop so the sync executor thread can schedule
    # the async send back on the bot's event loop.
    loop_for_send = asyncio.get_event_loop()

    def _send_file_sync(path: str, caption: str = "") -> None:
        async def _send():
            with open(path, "rb") as fh:
                await ctx.bot.send_document(
                    chat_id=chat_id, document=fh,
                    caption=(caption or "")[:1024],
                )
        fut = asyncio.run_coroutine_threadsafe(_send(), loop_for_send)
        fut.result(timeout=60)

    from ..tools.gateway_send_file import GatewaySendFile
    tools.add_tool(GatewaySendFile(send_fn=_send_file_sync))

    approver = make_protected(base_approver, caps, sess.mode_state.current)

    # Set up live indicators.
    loop = asyncio.get_event_loop()
    emitter = _make_telegram_emitter(chat_id, ctx.application, loop)
    on_step = _make_on_step(emitter)

    record: dict[str, Any] = {
        "ts": logger.now_iso(),
        "model": config.MODEL,
        "workspace": str(config.WORKSPACE),
        "request": req,
        "gateway": GATEWAY_NAME,
        "chat_id": chat_id,
        "mode": sess.mode_state.current,
    }

    # v1.5.1: continuous "typing…" pulse so the user sees activity during
    # long tool-call gather phases. Telegram's chat_action expires in ~5s,
    # so we re-pulse every 4s until the response is sent.
    typing_task = asyncio.create_task(
        _typing_pulse(ctx.bot, chat_id),
    )

    t0 = time.time()
    try:
        output, trace = await asyncio.to_thread(
            executor.chat,
            messages=sess.messages,
            user_input=req,
            tools=tools,
            approver=approver,
            on_step=on_step,
            memory_preamble=preamble,
            mode=sess.mode_state.current,
            workspace=str(config.WORKSPACE),
            tool_count=len(tools.names()),
            skill_count=len(skills.list_skills()),
            stream=False,
        )
        record["execute_ms"] = int((time.time() - t0) * 1000)
        record["output"] = output
        record["trace"] = trace
    except Exception as e:
        typing_task.cancel()
        await ctx.bot.send_message(chat_id=chat_id, text=f"chat failed: {e}")
        record["error"] = str(e)
        logger.write(record)
        return
    finally:
        typing_task.cancel()

    logger.write(record)
    sess.save()  # persist messages + mode after successful turn

    # v1.3 L3 #2 — per-chat cost ledger.
    try:
        ts = cost.turn_stats()
        cost.record_per_chat(
            gateway=GATEWAY_NAME, chat_id=str(chat_id),
            identity=gw.identity_for(GATEWAY_NAME, str(chat_id)) or "",
            model=config.MODEL,
            prompt_tokens=ts.prompt_tokens,
            completion_tokens=ts.completion_tokens, usd=ts.usd,
        )
    except Exception:
        pass

    # Memory diff proposal — emit indicator if any ops, then ask in console.
    # (Telegram-side review with inline keyboard is L3; for now, log only.)
    try:
        ops = memory.propose_diff(req, output)
        if ops:
            emitter.memory_update(
                len(ops),
                summary=", ".join(
                    f"{op.get('category', 'user')}.{op.get('section', '?')}"
                    for op in ops[:3]
                ),
            )
    except Exception:
        pass

    try:
        index.sync()
    except Exception:
        pass

    await _send(ctx.bot, chat_id, output or "(no output)")


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """4-button approval keyboard taps."""
    q = update.callback_query
    await q.answer()
    if not _is_authorized(update.effective_chat.id):
        return
    data = q.data or ""

    if not data.startswith("appr:"):
        return
    _, token, choice = data.split(":", 2)
    sess = _session(update.effective_chat.id)
    fut = sess.approval_futures.pop(token, None)
    cap_key = sess.approval_futures.pop(token + ".key", None)

    if fut is None or fut.done():
        return

    granted = choice in ("once", "sess", "always")
    if choice == "sess" and cap_key:
        sess.session_grants.add(str(cap_key))
    if choice == "always" and cap_key:
        sess.grant_always(str(cap_key))

    fut.set_result(granted)
    label = {
        "once": "approved (this call only)",
        "sess": "approved (this session)",
        "always": "approved (always)",
        "deny": "denied",
    }.get(choice, "")
    try:
        await q.edit_message_text(q.message.text + f"\n\n→ {label}")
    except Exception:
        pass


# ---------- Public entry point ----------


async def _post_init(app) -> None:
    """v1.3.2: register the slash-command menu with Telegram so the `/`
    autocomplete UI shows our commands.

    Without this, commands work when TYPED but never appear in the
    suggestion popup — which is what made v1.3 feel broken even after
    the upgrade.
    """
    try:
        await app.bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in _BOT_COMMANDS]
        )
    except Exception:
        # Best-effort — never block startup on Telegram API hiccups.
        pass


def serve() -> None:
    if not HAVE_TG:
        raise SystemExit(
            "python-telegram-bot is not installed.\n"
            "  pipx install '/opt/quantumapex/janus[telegram]'   # or [all]\n"
            "  pipx inject janus-agent 'python-telegram-bot>=20' # if already installed"
        )
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "JANUS_TELEGRAM_TOKEN not set.\n"
            "  export JANUS_TELEGRAM_TOKEN='123456:ABCdef…'\n"
            "  (chat access via `janus pair approve <CODE>` per chat)"
        )

    config.assert_configured()
    config.ensure_home()

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("logo", cmd_logo))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("sethome", cmd_sethome))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("swarm", cmd_swarm))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # v1.5.1: photo + document handlers so attachments don't silently disappear.
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_callback))

    print(f"janus telegram gateway running ({branding.VERSION}). ctrl-c to stop.")
    app.run_polling(allowed_updates=["message", "callback_query"])
