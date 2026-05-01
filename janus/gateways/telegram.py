"""
gateways/telegram.py — v1.0 chat-shaped Telegram gateway.

ARCHITECTURE:
Telegram is just another chat surface for executor.chat(). Per-chat
Session keeps a `messages` list across messages so the model has
conversation context. No interpretation picker (that pre-1.0 affordance
moved behind /why). Permission mode is per-chat — switch with /mode.

Approval prompts still use inline keyboards: when the active mode says
ASK for a tool's risk class, the bot sends a y/N keyboard and awaits
the user's tap. ALLOW runs silently; DENY returns a refusal observation
to the model so it can adapt.

SECURITY:
Only chat IDs in JANUS_TELEGRAM_CHATS (comma-separated) are served. Any
other chat is silently ignored.

Skills attached via Telegram are still capability-token bounded. The bot
does not unlock anything the CLI can't do.

USE:
  python -m janus telegram

WHAT WE DON'T DO:
- Long messages stream as a single typing animation. We just chunk + send.
- File uploads. Out of scope for v1.
- Group chats with multiple humans authoring requests. Single-user model.
"""

from __future__ import annotations
import asyncio
import time
import uuid
from typing import Any

from .. import config, executor, logger, memory, index, skills, permissions
from .. import branding
from ..tools import default_registry, make_capability_aware, CapabilitySet


try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, CallbackQueryHandler,
        ContextTypes, filters,
    )
    HAVE_TG = True
except ImportError:  # pragma: no cover
    HAVE_TG = False


MAX_MSG = 3500  # leave headroom under Telegram's 4096


# ---------- Per-chat session ----------


class Session:
    """Per-chat conversation state.

    `messages` is the running chat history fed to executor.chat(). Survives
    across messages from the same chat so the model has context.

    `mode_state` is the per-chat permission mode. Defaults to whatever
    JANUS_APPROVAL says at session creation. /mode swaps it.
    """
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.messages: list[dict] = []
        self.mode_state = permissions.ModeState(
            current=permissions.normalize(config.APPROVAL_MODE)
        )
        self.approval_futures: dict[str, asyncio.Future] = {}


SESSIONS: dict[int, Session] = {}


def _allowed_chats() -> set[int]:
    raw = config.TELEGRAM_ALLOWED_CHATS or ""
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def _is_authorized(chat_id: int) -> bool:
    allowed = _allowed_chats()
    return not allowed or chat_id in allowed


def _session(chat_id: int) -> Session:
    s = SESSIONS.get(chat_id)
    if s is None:
        s = Session(chat_id)
        SESSIONS[chat_id] = s
    return s


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


async def _send(update_or_ctx, chat_id: int, text: str) -> None:
    bot = update_or_ctx.bot if hasattr(update_or_ctx, "bot") else update_or_ctx
    for chunk in _chunks(text):
        await bot.send_message(chat_id=chat_id, text=chunk)


# ---------- Mode-aware approver ----------


def _make_approver(chat_id: int, app, sess: Session):
    """v1.0 approver: consults the chat's permission mode + tool risk class.

    ALLOW → return True silently.
    DENY  → return False (the model sees the refusal observation).
    ASK   → send a y/N inline keyboard and await the user's tap.
    """
    def approver(action_label: str, details: str, **kw) -> bool:
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        decision = permissions.decide(risk, sess.mode_state.current)

        if decision == permissions.ALLOW:
            return True
        if decision == permissions.DENY:
            return False

        # ASK — inline keyboard.
        loop = asyncio.get_event_loop()
        token = uuid.uuid4().hex[:8]
        fut: asyncio.Future = loop.create_future()
        sess.approval_futures[token] = fut

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ approve", callback_data=f"appr:{token}:y"),
            InlineKeyboardButton("✗ deny",    callback_data=f"appr:{token}:n"),
        ]])
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


# ---------- Handlers ----------


def _logo_block() -> str:
    """Bifurcation logo + version + tagline, wrapped in a Telegram code
    fence so monospace box-drawing renders correctly on every client."""
    body = "\n".join(branding.LOGO_LINES)
    return (
        f"```\n{body}\n```\n"
        f"*janus*  v{branding.VERSION}\n"
        f"_{branding.TAGLINE}_"
    )


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    sess = _session(update.effective_chat.id)
    body = (
        f"{_logo_block()}\n\n"
        "telegram gateway online.\n"
        "send any text → chat with the agent.\n\n"
        f"*current mode:* {sess.mode_state.current}\n\n"
        "*commands*\n"
        "/mode /skills /memory /search <q> /logo /eval"
    )
    await update.message.reply_text(body, parse_mode="Markdown")


async def cmd_logo(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    await update.message.reply_text(_logo_block(), parse_mode="Markdown")


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    sess = _session(update.effective_chat.id)
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
    msg = f"mode → *{sess.mode_state.current}*"
    if normalized == permissions.BYPASS:
        msg += "\n\n⚠ every tool will run without asking."
    await update.message.reply_text(msg, parse_mode="Markdown")
    logger.write({
        "ts": logger.now_iso(),
        "type": "mode_switch",
        "gateway": "telegram",
        "chat_id": update.effective_chat.id,
        "new_mode": normalized,
    })


async def cmd_skills(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    items = skills.list_skills()
    if not items:
        await update.message.reply_text("no skills yet.")
        return
    lines = [f"• {s.name} ({s.state}) — {s.description}" for s in items]
    await _send(update.effective_chat, update.effective_chat.id, "\n".join(lines))


async def cmd_memory(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    txt = memory.read() or "(empty)"
    await _send(update.effective_chat, update.effective_chat.id, txt)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
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
    """Reset this chat's conversation state (messages list)."""
    if not _is_authorized(update.effective_chat.id):
        return
    sess = _session(update.effective_chat.id)
    sess.messages = []
    await update.message.reply_text("conversation cleared.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """v1.0 chat-shaped handler. No interpretation picker — straight to
    executor.chat() with the per-chat messages list."""
    if not _is_authorized(update.effective_chat.id):
        return
    chat_id = update.effective_chat.id
    sess = _session(chat_id)
    req = update.message.text or ""
    if not req.strip():
        return

    preamble = memory.prepend_for_prompt()

    base_approver = _make_approver(chat_id, ctx.application, sess)
    caps = CapabilitySet()
    tools = default_registry(capabilities=caps)
    approver = make_capability_aware(base_approver, caps)

    record: dict[str, Any] = {
        "ts": logger.now_iso(),
        "model": config.MODEL,
        "workspace": str(config.WORKSPACE),
        "request": req,
        "gateway": "telegram",
        "chat_id": chat_id,
        "mode": sess.mode_state.current,
    }

    t0 = time.time()
    try:
        output, trace = await asyncio.to_thread(
            executor.chat,
            messages=sess.messages,
            user_input=req,
            tools=tools,
            approver=approver,
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
        await ctx.bot.send_message(chat_id=chat_id, text=f"chat failed: {e}")
        record["error"] = str(e)
        logger.write(record)
        return

    logger.write(record)
    try:
        index.sync()
    except Exception:
        pass

    await _send(ctx, chat_id, output or "(no output)")


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Approval keyboard taps. Interpretation-pick callbacks (interp:*)
    are gone in v1.0 — only `appr:*` remains."""
    q = update.callback_query
    await q.answer()
    if not _is_authorized(update.effective_chat.id):
        return
    data = q.data or ""

    if data.startswith("appr:"):
        _, token, ans = data.split(":", 2)
        sess = _session(update.effective_chat.id)
        fut = sess.approval_futures.pop(token, None)
        if fut and not fut.done():
            fut.set_result(ans == "y")
        await q.edit_message_text(
            q.message.text + f"\n\n→ {'approved' if ans == 'y' else 'denied'}"
        )
        return


# ---------- Public entry point ----------


def serve() -> None:
    if not HAVE_TG:
        raise SystemExit(
            "python-telegram-bot is not installed.\n"
            "  pip install 'python-telegram-bot>=20'"
        )
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "JANUS_TELEGRAM_TOKEN not set.\n"
            "  export JANUS_TELEGRAM_TOKEN='123456:ABCdef…'\n"
            "  export JANUS_TELEGRAM_CHATS='1234567,8901234'   # allowlist"
        )

    config.assert_configured()
    config.ensure_home()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("logo", cmd_logo))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    print("janus telegram gateway running. ctrl-c to stop.")
    app.run_polling(allowed_updates=["message", "callback_query"])
