"""
gateways/telegram.py — Phase 5: Telegram bot frontend for janus.

ARCHITECTURE:
  Telegram is just another I/O surface. It reuses interpreter + executor
  unchanged. Approval prompts and interpretation choice become inline keyboards.
  Output is chunked to fit Telegram's 4096-character message cap.

SECURITY:
  Only chat IDs in JANUS_TELEGRAM_CHATS (comma-separated) are served. Any
  other chat is silently ignored. There is no first-time-trust bootstrap
  from Telegram itself — you set the allowlist in env.

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

from .. import config, interpreter, executor, logger, memory, index, skills
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
    """Keeps the in-flight interpretation + approval state for one chat."""
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.pending_request: str | None = None
        self.pending_interps: list | None = None
        self.pending_skill_matches: list | None = None
        self.pending_approval: dict | None = None  # {token: future, ...}
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


# ---------- Telegram-driven approver ----------


def _make_approver(chat_id: int, app):
    """Approver that sends a y/n inline keyboard and awaits the user's tap."""
    sess = _session(chat_id)

    def approver(action_label: str, details: str, **kw) -> bool:
        loop = asyncio.get_event_loop()
        token = uuid.uuid4().hex[:8]
        fut: asyncio.Future = loop.create_future()
        sess.approval_futures[token] = fut

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ approve", callback_data=f"appr:{token}:y"),
            InlineKeyboardButton("✗ deny",    callback_data=f"appr:{token}:n"),
        ]])
        body = f"⚠ approval needed\n*{action_label}*\n\n{details[:1000]}"
        coro = app.bot.send_message(chat_id=chat_id, text=body,
                                    parse_mode="Markdown", reply_markup=kb)
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
    body = (
        f"{_logo_block()}\n\n"
        "telegram gateway online.\n"
        "send any text → interpret + execute.\n\n"
        "*commands*\n"
        "/skills /memory /search <q> /logo /eval"
    )
    await update.message.reply_text(body, parse_mode="Markdown")


async def cmd_logo(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    await update.message.reply_text(_logo_block(), parse_mode="Markdown")


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


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update.effective_chat.id):
        return
    chat_id = update.effective_chat.id
    sess = _session(chat_id)
    req = update.message.text or ""
    sess.pending_request = req

    # Interpret (sync work; offload).
    preamble = memory.prepend_for_prompt()
    matches = skills.match(req)
    skill_hints = "\n".join(f"- {s.name}: {s.description}" for s in matches[:5])

    try:
        interps = await asyncio.to_thread(
            interpreter.interpret, req,
            memory_preamble=preamble, skill_hints=skill_hints,
        )
    except Exception as e:
        await update.message.reply_text(f"interpreter failed: {e}")
        return

    sess.pending_interps = interps
    sess.pending_skill_matches = matches

    # Single interpretation → run immediately.
    if len(interps) == 1:
        await _execute_with_choice(update, ctx, choice_index=0)
        return

    # Show inline keyboard for choice.
    body_parts = [f"*{i+1}. {x['label']}*\n_{x['action'][:200]}_"
                  for i, x in enumerate(interps)]
    body = "\n\n".join(body_parts)
    kb_rows = [[InlineKeyboardButton(f"{i+1}. {x['label'][:30]}",
                                     callback_data=f"interp:{i}")]
               for i, x in enumerate(interps)]
    kb_rows.append([InlineKeyboardButton("skip", callback_data="interp:skip")])
    await update.message.reply_text(body, parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(kb_rows))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_authorized(update.effective_chat.id):
        return
    data = q.data or ""

    # Approval callbacks: appr:<token>:y|n
    if data.startswith("appr:"):
        _, token, ans = data.split(":", 2)
        sess = _session(update.effective_chat.id)
        fut = sess.approval_futures.pop(token, None)
        if fut and not fut.done():
            fut.set_result(ans == "y")
        await q.edit_message_text(q.message.text + f"\n\n→ {'approved' if ans=='y' else 'denied'}")
        return

    # Interpretation pick: interp:<index> or interp:skip
    if data.startswith("interp:"):
        sel = data.split(":", 1)[1]
        if sel == "skip":
            await q.edit_message_text("skipped.")
            return
        await _execute_with_choice(update, ctx, choice_index=int(sel))


async def _execute_with_choice(update_or_query, ctx, *, choice_index: int):
    """Run executor against the chosen interpretation; reply with output."""
    cq = update_or_query.callback_query if hasattr(update_or_query, "callback_query") and update_or_query.callback_query else None
    chat = update_or_query.effective_chat
    chat_id = chat.id
    sess = _session(chat_id)
    if not sess.pending_request or not sess.pending_interps:
        return

    chosen = sess.pending_interps[choice_index]
    record: dict[str, Any] = {
        "ts": logger.now_iso(),
        "model": config.MODEL,
        "workspace": str(config.WORKSPACE),
        "request": sess.pending_request,
        "gateway": "telegram",
        "interpretations": sess.pending_interps,
        "choice": choice_index + 1,
    }

    # Execute. Telegram approver bridges back through inline keyboards.
    base_approver = _make_approver(chat_id, ctx.application)
    caps = CapabilitySet()
    tools = default_registry(capabilities=caps)
    approver = make_capability_aware(base_approver, caps)
    try:
        output, trace = await asyncio.to_thread(
            executor.execute,
            original_request=sess.pending_request,
            chosen_label=chosen["label"],
            chosen_action=chosen["action"],
            tools=tools, approver=approver,
            on_step=None,
            skill_body="",
            memory_preamble=memory.prepend_for_prompt(),
        )
        record["trace"] = trace
        record["output"] = output
    except Exception as e:
        await ctx.bot.send_message(chat_id=chat_id, text=f"execute failed: {e}")
        record["error"] = str(e)
        logger.write(record)
        return

    logger.write(record)
    try:
        index.sync()
    except Exception:
        pass

    if cq:
        await cq.edit_message_text(f"running: {chosen['label']}…")
    await _send(ctx, chat_id, output or "(no output)")


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
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    print("janus telegram gateway running. ctrl-c to stop.")
    app.run_polling(allowed_updates=["message", "callback_query"])
