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
import concurrent.futures
import time
import uuid
import logging
import os as _os
from typing import Any

from .. import app as janus_app  # `app` collides with python-telegram-bot Application
from .. import config, executor, logger, memory, index, skills, permissions
from .. import branding, cost
from ..tools import default_registry, make_protected, CapabilitySet
from . import _common as gw


# v1.31.8 — module-level logger for the telegram gateway. Field-validation
# finding from Sam's VPS: callbacks (button taps) and text messages were
# being CONSUMED by the bot but processed silently — handler exceptions
# eaten by ``except Exception: pass`` blocks, no audit trail. Pre-v1.31.8
# the only output channel was print() via stdout, which nohup captured
# but Janus never wrote to. Adding a proper Python logger so future
# silent failures surface in stderr (and the nohup log) instead of
# requiring py-spy archaeology to diagnose.
#
# Level controllable via JANUS_TELEGRAM_LOG_LEVEL env var (default
# WARNING — keeps normal operation quiet but exposes errors).
_LOG_LEVEL = _os.environ.get("JANUS_TELEGRAM_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.WARNING),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("janus.telegram")


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
    ("interview", "fill memory cards Q&A-style: /interview [<cat>|daily [N]|pause|about-me]"),
    ("cost",    "per-chat cost ledger"),
    ("search",  "search prior interactions in the log index"),
    ("swarm",   "agent swarms — list | describe | run | status | cancel"),
    ("clear",   "reset this chat's conversation"),
    ("logo",    "print the bifurcation logo"),
]


MAX_MSG = 3500  # leave headroom under Telegram's 4096
GATEWAY_NAME = "telegram"

# v1.18.2 — Telegram-specific tone/behavior preamble. Prepended above the
# memory block when running inside the Telegram chat loop. Sam's
# 2026-05-06 feedback: "Janus should be more friendly and do reactions
# on my text". This prompt addresses both — friendliness shape and
# pointing the model at telegram_react for lightweight acknowledgments.
TELEGRAM_FRIENDLINESS_PROMPT = """\
# You are running INSIDE a Telegram chat with the user.

Tone shifts vs the CLI:
- Be conversational. Greetings get warm short greetings back, not a
  tool dump.
- For lightweight acknowledgments ("got it", "sounds good", "cool"),
  use the `telegram_react` tool to react with an emoji INSTEAD OF
  sending a separate "✓ noted" message. Saves user attention.
- Use `telegram_react` proactively when the user shares something
  notable: 👍 for confirmations, ❤️ / 🥰 for personal info shared,
  🔥 for an interesting idea, ✅ for "task done", 🤔 for "let me
  think". One reaction is plenty per message.
- DON'T over-react. Reactions are seasoning, not the main course.
  If the user asks a real question or makes a real request, reply
  with the actual answer/work — not just a reaction.
- Keep replies tight. Telegram is mobile-first; each message you send
  is a notification on the user's phone. Two short messages > one
  paragraph.
- DON'T narrate tool calls ("I'll search memory now…"). The trace
  panel already shows tool activity. Just speak human."""

# v1.18.1: how long an approval prompt stays "live" before timing out.
# Pre-v1.18.1 was 180s — way too short for real Telegram UX (notification
# delays, phone unlocks, multitasking). Late clicks hit a cancelled
# future and on_callback returned silently with NO visible feedback,
# making buttons appear to "do nothing".
APPROVAL_TIMEOUT_S = int(__import__("os").environ.get(
    "JANUS_TELEGRAM_APPROVAL_TIMEOUT", "1800"
))
# Same constant for clarify (model asks user a question via inline KB).
CLARIFY_TIMEOUT_S = int(__import__("os").environ.get(
    "JANUS_TELEGRAM_CLARIFY_TIMEOUT", "1800"
))


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
        # v1.8.0 — clarify-tool futures, same pattern as approval. The
        # callback registers a future + token, sends the keyboard, blocks
        # via run_coroutine_threadsafe; on_callback / on_text resolves it.
        self.clarify_futures: dict[str, asyncio.Future] = {}
        # Session-scoped grants — cleared on /clear or process restart.
        # Keyed by "tool.verb" → True.
        self.session_grants: set[str] = set()
        # Always-grants — persisted to session file so they survive restart.
        # Stored under extras["always_grants"].
        self.always_grants: set[str] = set(
            self._persistent.extras.get("always_grants") or []
        )
        # v1.18.2 — most-recent inbound message_id, so telegram_react
        # can target the user's last message.
        self.last_user_message_id: int | None = None
        # v1.31.10 — pending plan-review approval that can be resolved
        # via Y/N TEXT REPLY in addition to the inline keyboard.
        # Set by the approver when it sends a plan-review keyboard;
        # cleared after on_callback or on_text resolves the future.
        # Workaround for environments where Telegram callback_query
        # delivery is broken (Sam's VPS field test 2026-05-08:
        # messages reach the bot but button taps never produce
        # callback_query updates).
        # Tuple is (future, token) — token used for double-resolve guards.
        self.pending_plan_review: "tuple[asyncio.Future, str] | None" = None

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


def _make_approver(chat_id: int, app, sess: Session, loop: asyncio.AbstractEventLoop | None = None):
    """v1.3 approver with session+always grants and 4-button keyboard.

    ALLOW   → True silently (mode allows the risk class).
    DENY    → False (the model sees the refusal observation).
    ASK     → check session/always grants; else send 4-button keyboard.

    `loop` is the gateway's asyncio event loop, captured at construction
    time on the asyncio thread. Required because the approver itself
    runs on the executor thread (via asyncio.to_thread), where
    `asyncio.get_event_loop()` raises "no current event loop" in
    Python 3.10+ — v1.6.1 bug Sam hit when the model tried fs_write
    after a trigger fire and the approver crashed before reaching the
    keyboard. Falls back to the running loop only as a defensive
    last-resort (used by older callers that hadn't been updated yet).
    """
    def approver(action_label: str, details: str, **kw) -> bool:
        # v1.31.5 — detect plan action FIRST. Field-validation finding
        # from Sam's VPS Telegram session: the model called
        # exit_plan_mode, the approver auto-ALLOWed (ExitPlanMode is
        # risk=read which permissions.decide(read, *) ALLOWs in every
        # mode), and the v1.30.0 📋 Plan Review message + 2-button
        # keyboard never rendered. Same root cause as the cli_rich
        # bug. Fix: gate the mode-based ALLOW/DENY/grants short-circuits
        # on is_plan == False. Plan actions ALWAYS proceed to the
        # render+keyboard flow regardless of mode. ``is_plan_action``
        # from plan_render is the shared detector (same one telegram +
        # cli_rich + web all use).
        try:
            from .. import plan_render as _plan_render_top
            is_plan = _plan_render_top.is_plan_action(action_label)
        except Exception:
            is_plan = bool(
                action_label
                and "exit_plan_mode" in action_label.lower()
            )

        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        cap = kw.get("capability") or (None, "", None)
        cap_key = f"{cap[0]}.{cap[1]}" if cap[0] else action_label

        if not is_plan:
            decision = permissions.decide(risk, sess.mode_state.current)
            if decision == permissions.ALLOW:
                return True
            if decision == permissions.DENY:
                return False

            # ASK — check pre-existing grants.
            if cap_key in sess.always_grants or cap_key in sess.session_grants:
                return True

        # Send 4-button keyboard.
        # Use the loop captured at construction time. Falls back to a
        # best-effort lookup so older callers don't break, but the modern
        # caller in _run_chat_turn always passes loop explicitly.
        approver_loop = loop
        if approver_loop is None:
            try:
                approver_loop = asyncio.get_event_loop()
            except RuntimeError:
                # No event loop in this thread (we're on the executor
                # thread). Treat ASK as DENY rather than crashing — the
                # model gets a refusal observation it can adapt to.
                return False
        token = uuid.uuid4().hex[:8]
        fut: asyncio.Future = approver_loop.create_future()
        # Stash key alongside future so the callback knows what to grant.
        sess.approval_futures[token] = fut
        sess.approval_futures[token + ".key"] = cap_key  # type: ignore

        # v1.30.0 — structured plan-review rendering for ExitPlanMode.
        # Symmetric to v1.27.2 cli_rich + v1.29.3 auto-offer parity.
        # Plan approvals get a 2-button (Yes/Deny) keyboard intentionally:
        # every plan deserves a fresh decision (no session/always grants).
        # v1.31.5 — ``is_plan`` is computed at the top of approver()
        # so plan actions ALWAYS reach this branch regardless of mode.
        if is_plan:
            try:
                from .. import plan_render as _plan_render
                parsed = _plan_render.parse_plan(details)
                body = _plan_render.render_telegram_text(
                    parsed, details, mode=sess.mode_state.current,
                )
            except Exception:
                # Fall back to the generic body if parsing/rendering hiccups.
                body = (
                    f"📋 Plan Review (mode={sess.mode_state.current})\n\n"
                    f"{details[:3500]}"
                )
            # v1.31.10 — append Y/N text-reply fallback hint. The
            # buttons still work (when callback_query delivery is
            # functional). Text reply is the workaround for environments
            # where Telegram doesn't deliver callbacks.
            body += (
                "\n\n💬 OR reply Y (approve) / N (refine) "
                "if buttons don't work."
            )
            sess.pending_plan_review = (fut, token)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✓ Approve plan", callback_data=f"appr:{token}:once",
                    ),
                    InlineKeyboardButton(
                        "✗ Refine", callback_data=f"appr:{token}:deny",
                    ),
                ],
            ])
        else:
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
        # v1.31.7 — plan reviews go without parse_mode. Field-validation
        # finding from Sam's VPS: model-generated plan bodies frequently
        # contain markdown that Telegram's parse_mode="Markdown" can't
        # parse (`**bold**`, identifiers with underscores). The send
        # then fails silently on the asyncio loop and the approver
        # hangs on the future for 30 minutes (no message → no buttons →
        # no future-resolve). Plain text is the only safe choice for
        # unpredictable model output. Generic ASK approvals keep
        # Markdown — their body is constructed by us and is known
        # safe.
        plan_parse_mode = None if is_plan else "Markdown"
        coro = app.bot.send_message(
            chat_id=chat_id, text=body,
            parse_mode=plan_parse_mode, reply_markup=kb,
        )
        send_fut = asyncio.run_coroutine_threadsafe(coro, approver_loop)
        # v1.31.7: wait briefly for send to actually complete so a
        # parse error / network hiccup surfaces here instead of leaving
        # us hung on the future. If send fails, retry plain-text + log
        # the failure so future debugging isn't a black box.
        try:
            send_fut.result(timeout=10)
        except Exception as send_exc:
            try:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "telegram approval send_message failed (%s): %r — "
                    "retrying with plain text",
                    type(send_exc).__name__, send_exc,
                )
            except Exception:
                pass
            try:
                fallback_coro = app.bot.send_message(
                    chat_id=chat_id, text=body, reply_markup=kb,
                )
                asyncio.run_coroutine_threadsafe(
                    fallback_coro, approver_loop,
                ).result(timeout=10)
            except Exception:
                # Both Markdown + plain failed. The user won't see a
                # keyboard; the future will time out at
                # APPROVAL_TIMEOUT_S and the model gets a refusal.
                # Better than the pre-v1.31.7 silent-hang.
                pass
        # The approver runs on the executor thread, so we can't call
        # loop.run_until_complete (would crash — that's a main-loop
        # operation). Schedule the future on the gateway loop and
        # block this thread on the concurrent.futures.Future returned
        # by run_coroutine_threadsafe.
        #
        # v1.18.1: timeout bumped 180s → 1800s (30 min). 3 minutes was
        # too short for real Telegram UX (notification delays, phone
        # unlocks, multi-tasking). Pre-fix bug: user clicks AFTER the
        # 180s timeout fired, wait_for cancels the future, on_callback
        # finds fut.done() and returns SILENTLY with no UI feedback.
        # User sees buttons "do nothing".
        wait_coro = asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_S)
        wait_fut = asyncio.run_coroutine_threadsafe(wait_coro, approver_loop)
        try:
            return bool(wait_fut.result(timeout=APPROVAL_TIMEOUT_S + 5))
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
            # On timeout, edit the keyboard message so the user knows
            # the prompt is dead — otherwise a late click hits the
            # done() future and returns silently.
            async def _mark_expired() -> None:
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⏱ approval prompt for *{action_label}* expired "
                            f"after {APPROVAL_TIMEOUT_S // 60} min — "
                            f"action denied. Re-issue the request to retry."
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            try:
                asyncio.run_coroutine_threadsafe(
                    _mark_expired(), approver_loop,
                )
            except Exception:
                pass
            # Drop the future from the dict — late callbacks now see
            # missing-token rather than done-future. Both paths now
            # produce visible feedback in on_callback.
            sess.approval_futures.pop(token, None)
            sess.approval_futures.pop(token + ".key", None)
            return False
        except Exception:
            sess.approval_futures.pop(token, None)
            sess.approval_futures.pop(token + ".key", None)
            return False
    return approver


# ---------- Clarify keyboard (v1.8.0) ----------


def _make_telegram_clarify_cb(chat_id: int, app, sess: Session, loop: asyncio.AbstractEventLoop):
    """Callback for the clarify tool when invoked from a Telegram chat.

    Sends an inline keyboard (one button per choice + an OTHER button) or
    a free-text prompt, blocks the executor thread on a future the
    on_callback / on_text handler resolves. 5-minute timeout — the
    tool emits the UNAVAILABLE sentinel on timeout so the model picks
    a default and continues.
    """
    def callback(question: str, choices: list[str] | None) -> str | None:
        token = uuid.uuid4().hex[:8]
        fut: asyncio.Future = loop.create_future()
        sess.clarify_futures[token] = fut

        # v1.31.12 — plain text body (no markdown) for the same reason
        # as v1.31.7: model-generated questions might contain
        # underscores / asterisks that break Telegram's parse_mode
        # "Markdown". Plain text always delivers.
        body = f"❓ clarify\n\n{question[:1000]}"
        if choices:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb_rows = []
            row = []
            for i, c in enumerate(choices):
                row.append(InlineKeyboardButton(
                    c[:30], callback_data=f"clr:{token}:{i}",
                ))
                if len(row) == 2:
                    kb_rows.append(row)
                    row = []
            if row:
                kb_rows.append(row)
            kb_rows.append([InlineKeyboardButton(
                "✎ Other (type your answer)",
                callback_data=f"clr:{token}:other",
            )])
            kb = InlineKeyboardMarkup(kb_rows)
            # v1.31.12 — also accept ANY text reply as the answer,
            # not just after tapping "Other". Mirrors the plan-review
            # text-reply fallback (v1.31.10). Without this, environments
            # where Telegram callback_query delivery is broken can't
            # answer clarifies at all because they can't tap "Other"
            # to enter the text-reply path.
            sess.clarify_futures["__awaiting_text__"] = fut  # type: ignore
            body += (
                "\n\n💬 OR type the option text (or your own answer) "
                "if buttons don't work."
            )
            coro = app.bot.send_message(
                chat_id=chat_id, text=body, reply_markup=kb,
            )
        else:
            body += "\n\ntype your answer in the chat."
            coro = app.bot.send_message(
                chat_id=chat_id, text=body,
            )
            # Mark this token as awaiting free text — on_text resolves it.
            sess.clarify_futures["__awaiting_text__"] = fut  # type: ignore

        asyncio.run_coroutine_threadsafe(coro, loop)
        # v1.18.1: bumped 300s → CLARIFY_TIMEOUT_S (default 1800s).
        # Same root cause as approval timeout — humans need time.
        wait_coro = asyncio.wait_for(fut, timeout=CLARIFY_TIMEOUT_S)
        wait_fut = asyncio.run_coroutine_threadsafe(wait_coro, loop)
        try:
            answer = wait_fut.result(timeout=CLARIFY_TIMEOUT_S + 5)
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
            answer = None
        except Exception:
            answer = None
        finally:
            sess.clarify_futures.pop(token, None)
            sess.clarify_futures.pop("__awaiting_text__", None)
        if answer is None:
            # Send a follow-up so the user knows the prompt is dead.
            async def _mark_clarify_expired() -> None:
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⏱ clarify prompt expired after "
                            f"{CLARIFY_TIMEOUT_S // 60} min — "
                            f"agent proceeded without an answer."
                        ),
                    )
                except Exception:
                    pass
            try:
                asyncio.run_coroutine_threadsafe(
                    _mark_clarify_expired(), loop,
                )
            except Exception:
                pass
            return None
        if answer is None or not str(answer).strip():
            return None
        # If a numeric/index resolution came back, map to the choice text.
        s = str(answer)
        if choices and s.isdigit():
            i = int(s)
            if 0 <= i < len(choices):
                return choices[i]
        return s
    return callback


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
        # v1.31.16 — Hermes-style quiet by default. The model's final
        # assistant message carries the substantive output; intermediate
        # tool/skill/memory/thinking glyphs were noisy spam in field
        # use. Set JANUS_TELEGRAM_VERBOSE=1 to bring the glyph stream
        # back. Approval / clarify / plan-review prompts STILL fire —
        # those go through different paths (the approver callbacks),
        # not this emitter, so the user can always intervene.
        if not config.TELEGRAM_VERBOSE:
            return
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


async def cmd_interview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """v1.19.1 — /interview slash command on Telegram.

    Subcommands:
      /interview                    enable drip mode (10 questions/day,
                                     all categories) — effectively walks
                                     the full library one Q per turn
      /interview <category>         enable drip filtered to <category>
      /interview daily [N]          slow drip (N q/day, default 2)
      /interview pause              stop drip
      /interview about-me           list current memory snapshot

    Each user message in drip mode answers the pending question AND
    optionally chats normally (the answer is recorded as a high-
    confidence card without blocking the chat).
    """
    chat_id = update.effective_chat.id
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update)
        return
    arg = " ".join(ctx.args or []).strip()
    arg_low = arg.lower()

    from .. import interviews as _iv
    _iv.maybe_install_bundled()
    state = _iv.load_state("telegram", str(chat_id))

    # Pause / stop
    if arg_low in ("pause", "stop"):
        state.mode = "idle"
        state.drip_filter_category = ""
        state.current_question_id = ""
        _iv.save_state(state)
        await update.message.reply_text("interview paused.")
        return

    # /interview about-me — read-back of current memory
    if arg_low in ("about-me", "aboutme", "about me"):
        await _telegram_send_about_me(update, ctx, chat_id)
        return

    # Parse category vs daily vs default
    category_filter = ""
    per_day = 10  # one-shot-ish mode: walks library quickly
    msg_suffix = " (about all categories)"
    if arg_low.startswith("daily"):
        rest = arg[5:].strip()
        try:
            per_day = max(1, min(20, int(rest))) if rest else _iv.DRIP_DEFAULT_PER_DAY
        except ValueError:
            per_day = _iv.DRIP_DEFAULT_PER_DAY
        msg_suffix = " (slow drip)"
    elif arg_low in _iv.SUPPORTED_CATEGORIES:
        category_filter = arg_low
        msg_suffix = f" about {arg_low}"
    elif arg_low:
        await update.message.reply_text(
            f"usage: /interview [<category>|daily [N]|pause|about-me]\n"
            f"category: {', '.join(_iv.SUPPORTED_CATEGORIES)}"
        )
        return

    state.mode = "drip"
    state.drip_filter_category = category_filter
    if not state.started_at:
        state.started_at = _iv._now_iso()
    _iv.reset_drip_quota(state, per_day=per_day)
    _iv.save_state(state)

    await update.message.reply_text(
        f"🎯 interview mode on — Janus will ask up to {per_day} "
        f"question(s)/day{msg_suffix}.\n\n"
        f"Reply normally to answer, 'skip' to skip a question, "
        f"'stop drip' to pause."
    )


async def _telegram_send_about_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                  chat_id: int) -> None:
    """v1.19.1 — Telegram-side /interview about-me."""
    from .. import interviews as _iv, memory_index, memory_cards
    try:
        memory_index.reconcile()
    except Exception:
        pass
    rows = memory_index.list_all()
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)

    parts = ["*Here's what I know about you:*"]
    any_cards = False
    for cat in _iv.SUPPORTED_CATEGORIES:
        cat_rows = by_type.get(cat, [])
        if not cat_rows:
            continue
        any_cards = True
        parts.append(f"\n*{cat}*")
        from pathlib import Path
        for r in cat_rows[:10]:
            try:
                card = memory_cards.read_card(Path(r["path"]))
                content = card.content[:200].replace("\n", " ")
                parts.append(f"  • {r['subject']}: {content}")
            except Exception:
                continue
    if not any_cards:
        parts.append("\n_(nothing yet — try /interview to fill in your profile)_")
    else:
        parts.append("\n_anything wrong? reply with corrections._")
    await _send(ctx.bot, chat_id, "\n".join(parts))


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
    # v1.31.9 — handler-entry logging. Pairs with on_callback's FIRED
    # log so we can compare handler dispatch for messages vs callbacks
    # in field-validation logs.
    try:
        _txt_preview = (update.message.text or "")[:60] if update.message else ""
    except Exception:
        _txt_preview = ""
    log.info(
        "on_text FIRED chat=%s text=%r", chat_id, _txt_preview,
    )

    # Unauthorized → pairing flow.
    if not _is_authorized(chat_id):
        await _send_pairing_prompt(update)
        return

    sess = _session(chat_id)
    req = update.message.text or ""
    if not req.strip():
        return

    # v1.31.10 — if a plan-review approval is pending and the user
    # types a Y/N variant, resolve via text reply instead of waiting
    # for an inline-keyboard callback. Workaround for environments
    # where Telegram doesn't deliver callback_query updates (Sam's
    # VPS field test 2026-05-08: every kind of tap reproduced the
    # silent-no-delivery; button highlight visible client-side but
    # `getUpdates` never returns the callback_query).
    pending_pr = getattr(sess, "pending_plan_review", None)
    if pending_pr is not None:
        fut, _token = pending_pr
        if fut.done():
            # Already resolved (button click won the race) — clear
            # stale state and fall through to normal chat handling.
            sess.pending_plan_review = None
        else:
            normalized = req.strip().lower()
            approve_words = (
                "y", "yes", "approve", "approve plan", "ok", "go", "proceed",
            )
            refine_words = (
                "n", "no", "refine", "decline", "reject", "stop",
            )
            if normalized in approve_words:
                fut.set_result(True)
                sess.pending_plan_review = None
                log.info(
                    "on_text: plan APPROVED via text reply token=%s "
                    "text=%r", _token, normalized,
                )
                try:
                    await update.message.reply_text(
                        "✓ plan approved (via text reply)"
                    )
                except Exception as e:
                    log.warning("on_text: reply_text raised: %r", e)
                return
            if normalized in refine_words:
                fut.set_result(False)
                sess.pending_plan_review = None
                log.info(
                    "on_text: plan REFINED via text reply token=%s "
                    "text=%r", _token, normalized,
                )
                try:
                    await update.message.reply_text(
                        "✗ plan refined (via text reply)"
                    )
                except Exception as e:
                    log.warning("on_text: reply_text raised: %r", e)
                return
            # Not a Y/N reply — fall through to normal chat handling.
            # The plan-review keyboard stays live for the user to
            # tap (if their environment supports it) or to type Y/N
            # in a future message.

    # v1.8.0 — if a clarify-free-text future is pending, this incoming
    # message IS the answer (not a new chat turn). Resolve and bail
    # before the normal chat path runs.
    awaiting = sess.clarify_futures.pop("__awaiting_text__", None)
    if awaiting is not None and not awaiting.done():
        awaiting.set_result(req.strip())
        try:
            await update.message.reply_text(f"→ recorded: {req.strip()[:120]}")
        except Exception:
            pass
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
        greeting = gw.greeting(user_label)
        # v1.5.2: hint about /mode auto when default mode is active.
        # Default mode asks for approval per write/exec call, which on
        # Telegram means tapping the 4-button keyboard for every action.
        # Auto mode allows-with-risk-analysis: long tasks run without
        # interruption while rm -rf / and SSRF still block.
        if sess.mode_state.current == permissions.DEFAULT:
            greeting += (
                "\n\n💡 Tip: try `/mode auto` for fewer approval prompts. "
                "Tools auto-run, but dangerous ops (rm -rf /, fs writes "
                "to /etc/, SSRF) still block automatically."
            )
        await update.message.reply_text(greeting, parse_mode="Markdown")
        sess.mark_greeted()
        # If they said "hi"/"hello", the greeting is the whole reply.
        if req.lower().strip(" .!,?") in ("hi", "hello", "hey", "yo", "sup"):
            return

    await _run_chat_turn(update, ctx, chat_id, sess, req)

    # v1.37.3 — Phase 10.1.3: /goal Ralph Loop server-side auto-
    # continue. _run_chat_turn's goal hook queues the next prompt
    # onto sess._goal_next_prompt; we drain it here, capped at
    # JANUS_GOAL_ITERS_PER_MSG (default 5) per inbound user message
    # so a runaway loop can't burn the whole budget in one turn.
    # Cycle detection + budget exhaustion (in goal_loop.after_turn)
    # are the deeper safety nets — this cap is just a per-message
    # ceiling.
    try:
        max_iters = int(_os.environ.get("JANUS_GOAL_ITERS_PER_MSG", "5"))
    except ValueError:
        max_iters = 5
    iter_count = 0
    while iter_count < max_iters:
        next_prompt = getattr(sess, "_goal_next_prompt", None)
        if not next_prompt:
            break
        sess._goal_next_prompt = None  # clear before chaining so a
                                       # downstream judge can re-set it
        await _run_chat_turn(update, ctx, chat_id, sess, next_prompt)
        iter_count += 1


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
    # v1.18.2 — stash the user's message_id so telegram_react can target
    # the message we're replying to. Also stash the chat_id (already
    # known but easier than threading it through tool closures).
    try:
        sess.last_user_message_id = (
            update.message.message_id if update.message else None
        )
    except Exception:
        sess.last_user_message_id = None

    # v1.19.1 — drip-mode pre-turn: if a question is pending, treat
    # user's message as the answer. The user's input ALSO goes to the
    # executor as a normal chat turn, so they can both answer AND chat
    # in one message.
    try:
        from .. import interviews as _iv
        drip_handled, drip_ack = _iv.consume_pending_drip_answer(
            "telegram", str(chat_id), req,
        )
        if drip_handled and drip_ack:
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id, text=f"→ {drip_ack}",
                )
            except Exception:
                pass
    except Exception:
        pass

    preamble = memory.prepend_for_prompt()
    # v1.18.2 — Telegram-specific friendliness preamble. Prepended above
    # the static memory block so it's load-bearing for chat tone.
    preamble = TELEGRAM_FRIENDLINESS_PROMPT + "\n\n" + (preamble or "")

    # Capture the gateway's event loop ONCE on the asyncio thread, then
    # pass it everywhere downstream — the executor will run on a worker
    # thread (asyncio.to_thread) where get_event_loop() raises in
    # Python 3.10+. v1.6.1 fix.
    gateway_loop = asyncio.get_event_loop()
    base_approver = _make_approver(chat_id, ctx.application, sess, loop=gateway_loop)
    caps = CapabilitySet()
    tools = default_registry(capabilities=caps)
    # v1.5.1: register the gateway send-file tool so the model can deliver
    # files as attachments (not paste content). Closure captures the bot
    # + chat_id + asyncio loop so the sync executor thread can schedule
    # the async send back on the bot's event loop.
    loop_for_send = gateway_loop

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

    # v1.8.0: replace bundled callback-less Clarify with a Telegram-aware
    # one — sends an inline keyboard for choices, a free-text prompt
    # otherwise, and waits on a future the on_callback / on_text handler
    # resolves. Same pattern as the approval keyboard.
    from ..tools.clarify import Clarify as _Clarify
    tools.remove_tool("clarify")
    tools.add_tool(_Clarify(callback=_make_telegram_clarify_cb(
        chat_id, ctx.application, sess, gateway_loop,
    )))

    # v1.18.2 — Telegram-bound react tool. Closure captures chat_id and
    # the session's most-recent inbound message_id so the model only
    # needs to pass an emoji.
    from ..tools.telegram_react import TelegramReact as _TelegramReact
    def _msg_id_provider() -> tuple[int, int] | None:
        mid = sess.last_user_message_id
        if not mid:
            return None
        return (chat_id, int(mid))
    tools.remove_tool("telegram_react")
    tools.add_tool(_TelegramReact(msg_id_callback=_msg_id_provider))

    approver = make_protected(base_approver, caps, sess.mode_state.current)

    # Set up live indicators (reuse the gateway loop captured above).
    emitter = _make_telegram_emitter(chat_id, ctx.application, gateway_loop)
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

    # v1.10.0 — set origin in the executor thread so agent_create can
    # default deliver_to to telegram:<this_chat_id> without the model
    # having to look it up. Origin is threading.local so concurrent
    # chats stay isolated; we clear on exit (context manager).
    from .. import session_context as _ctx

    def _chat_with_origin():
        with _ctx.origin_context(
            platform="telegram",
            chat_id=str(chat_id),
            chat_name=update.effective_chat.title if update.effective_chat else None,
            user=_user_label(update),
        ):
            # v1.25.7 Phase 0e: route through the surface-agnostic
            # event stream (substrate is in janus/app.py since v1.25.0).
            return janus_app.run_turn(
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

    t0 = time.time()
    try:
        output, trace = await asyncio.to_thread(_chat_with_origin)
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
    # v1.18: also auto-applies typed cards (scoped to telegram:<chat_id> by
    # default per session_context.current_scope()).
    try:
        result = memory.propose_diff(req, output)
        ops = result.get("ops") or []
        cards = result.get("cards") or []
        if ops:
            emitter.memory_update(
                len(ops),
                summary=", ".join(
                    f"{op.get('category', 'user')}.{op.get('section', '?')}"
                    for op in ops[:3]
                ),
            )
        if cards:
            memory.apply_cards(cards, gateway="telegram")
    except Exception:
        pass

    try:
        index.sync()
    except Exception:
        pass

    await _send(ctx.bot, chat_id, output or "(no output)")

    # v1.19.1 — drip-mode post-turn + inferred-suggestion display.
    # Best-effort; never break the chat reply.
    try:
        from .. import interviews as _iv
        drip_q = _iv.get_drip_question("telegram", str(chat_id))
        if drip_q is not None:
            question_text, _fqid = drip_q
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"💬 *Quick question:* {question_text}\n\n"
                        f"_(answer normally, 'skip' to skip, "
                        f"'stop drip' to pause)_"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                # Markdown parse failure → send plain.
                try:
                    await ctx.bot.send_message(
                        chat_id=chat_id,
                        text=f"💬 Quick question: {question_text}",
                    )
                except Exception:
                    pass
    except Exception:
        pass

    try:
        from .. import interview_inferred as _inf
        offer = _inf.pop_pending("telegram", str(chat_id))
        if offer is not None:
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=f"💡 {_inf.render_offer(offer)}",
                )
            except Exception:
                pass
    except Exception:
        pass

    # v1.29.3: skill auto-offer parity (extends v1.28.1 cli_rich-only).
    # Same gates as cli_rich: top pattern only, AUTO_OFFER_MIN_OCCURRENCES
    # threshold, mark_offered triggers cooldown. Best-effort wrap so
    # detection bugs never break the chat reply.
    try:
        from .. import skill_proposer as _sp
        patterns = _sp.list_offerable(current_trace=trace)
        if patterns:
            top = patterns[0]
            if top.occurrences >= _sp.AUTO_OFFER_MIN_OCCURRENCES:
                try:
                    await ctx.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🪄 {top.description}.\n"
                            f"/skills propose {top.id} to draft, "
                            f"/skills decline {top.id} to silence."
                        ),
                    )
                    _sp.mark_offered(top.id)
                except Exception:
                    pass
    except Exception:
        pass

    # v1.37.2 — Phase 10.1.2: /goal Ralph Loop post-turn hook on
    # telegram. State tracking + UI notifications only — auto-
    # continue execution (recursive turn chaining with iter cap)
    # arrives in v1.37.3. For now, when the judge declares the
    # goal achieved or auto-pauses (cycle/budget), the bot sends a
    # one-line notification and the loop stops there. The user
    # can manually continue with another message; turns_used and
    # cycle detection still increment correctly.
    try:
        from .. import goal_loop as _gl
        _scope = f"telegram:{chat_id}"
        _decision = _gl.after_turn(_scope, output or "")
        # v1.37.4 — budget alert at 50/80/100% before the verdict.
        if _decision.budget_alert is not None:
            _pct = int(_decision.budget_alert * 100)
            _spent = (
                f" (spent ${_decision.cost_usd:.4f})"
                if _decision.cost_usd > 0 else ""
            )
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠ goal budget {_pct}% used{_spent}",
                )
            except Exception:
                pass
        if _decision.achieved:
            _spent_str = (
                f" (spent ${_decision.cost_usd:.4f})"
                if _decision.cost_usd > 0 else ""
            )
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✓ goal achieved: {_decision.reason}{_spent_str}"
                    ),
                )
            except Exception:
                pass
        elif _decision.paused:
            _marker = "cycle" if _decision.cycle_detected else (
                "budget" if _decision.budget_exhausted else "paused"
            )
            _spent_str = (
                f" (spent ${_decision.cost_usd:.4f})"
                if _decision.cost_usd > 0 else ""
            )
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏸ goal paused ({_marker}): "
                        f"{_decision.reason}{_spent_str}\n"
                        f"_/goal resume to continue, /goal clear to drop_"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        elif _decision.next_prompt:
            # v1.37.3: queue the next prompt onto the session so the
            # outer on_text loop can chain another _run_chat_turn
            # invocation (server-side auto-continue). Capped per-
            # message by JANUS_GOAL_ITERS_PER_MSG to prevent runaway.
            try:
                sess._goal_next_prompt = _decision.next_prompt
                preview = _decision.next_prompt
                if len(preview) > 200:
                    preview = preview[:197] + "..."
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=f"→ goal continuing: _{preview}_",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    except Exception:
        pass


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Inline-keyboard taps: approval (`appr:`) and clarify (`clr:`).

    v1.18.1: when the future is missing OR done (timed out / process
    restarted / already clicked), give VISIBLE feedback via
    ``q.answer(text=..., show_alert=True)`` instead of returning
    silently. Pre-fix bug: clicks after the 180s timeout did nothing
    visible — user thought buttons were broken (Sam's screenshot
    2026-05-06).

    v1.31.8: instrumented with logger.info at each branch + decision
    point so future silent failures show up in the log instead of
    requiring py-spy to diagnose.
    """
    q = update.callback_query
    chat_id = update.effective_chat.id if update.effective_chat else None
    data = (q.data if q else None) or ""
    log.info(
        "on_callback FIRED chat=%s data=%r", chat_id, data[:80],
    )
    # q.answer() may carry a popup. We DON'T answer yet — we want to
    # potentially answer with text/alert for stale clicks.
    if not _is_authorized(update.effective_chat.id):
        log.warning("on_callback: chat %s not authorized — refusing", chat_id)
        try:
            await q.answer(text="not authorized", show_alert=True)
        except Exception:
            pass
        return
    data = q.data or ""

    sess = _session(update.effective_chat.id)

    # v1.8.0 — clarify keyboard taps.
    if data.startswith("clr:"):
        try:
            _, token, choice = data.split(":", 2)
        except ValueError:
            try:
                await q.answer()
            except Exception:
                pass
            return
        fut = sess.clarify_futures.pop(token, None)
        if fut is None or fut.done():
            try:
                await q.answer(
                    text="this question expired — re-ask if needed",
                    show_alert=True,
                )
            except Exception:
                pass
            return
        if choice == "other":
            # User wants to type a free-text answer instead. Re-mark
            # the future as awaiting text and tell them so.
            sess.clarify_futures["__awaiting_text__"] = fut
            try:
                await q.answer(text="type your answer in chat")
            except Exception:
                pass
            try:
                await q.edit_message_text(
                    (q.message.text or "")
                    + "\n\n→ type your answer in the chat."
                )
            except Exception:
                pass
            return
        # Numeric index → resolve immediately (callback maps to choice text).
        fut.set_result(choice)
        try:
            await q.answer(text=f"chose option {int(choice) + 1}")
        except Exception:
            pass
        try:
            await q.edit_message_text(
                (q.message.text or "")
                + f"\n\n→ chose option {int(choice) + 1}"
            )
        except Exception:
            pass
        return

    if not data.startswith("appr:"):
        try:
            await q.answer()
        except Exception:
            pass
        return
    try:
        _, token, choice = data.split(":", 2)
    except ValueError:
        try:
            await q.answer()
        except Exception:
            pass
        return
    fut = sess.approval_futures.pop(token, None)
    cap_key = sess.approval_futures.pop(token + ".key", None)
    log.info(
        "on_callback appr: token=%s choice=%s fut=%s done=%s",
        token, choice,
        "found" if fut is not None else "MISSING",
        fut.done() if fut is not None else "n/a",
    )

    if fut is None or fut.done():
        # Stale click — prompt expired (timeout fired or process
        # restarted). Show the user a popup so they know the click
        # registered but the request is gone.
        log.warning(
            "on_callback: stale click token=%s (fut %s) — showing expired popup",
            token, "missing" if fut is None else "already done",
        )
        try:
            await q.answer(
                text="this approval prompt expired — re-issue the request",
                show_alert=True,
            )
        except Exception as e:
            log.warning("on_callback: q.answer (stale) raised: %r", e)
        # Best-effort: edit the message so it's clear the buttons are dead.
        try:
            await q.edit_message_text(
                (q.message.text or "(approval prompt)") + "\n\n→ (expired)"
            )
        except Exception as e:
            log.warning("on_callback: edit_message (stale) raised: %r", e)
        return

    granted = choice in ("once", "sess", "always")
    if choice == "sess" and cap_key:
        sess.session_grants.add(str(cap_key))
    if choice == "always" and cap_key:
        sess.grant_always(str(cap_key))

    fut.set_result(granted)
    log.info(
        "on_callback: fut.set_result(%s) for token=%s — chat-turn should wake",
        granted, token,
    )
    # v1.31.10 — clear the text-reply fallback marker so a stray
    # later Y/N text doesn't try to re-resolve a done future.
    if getattr(sess, "pending_plan_review", None) is not None:
        pr_fut, pr_token = sess.pending_plan_review
        if pr_token == token:
            sess.pending_plan_review = None
    label = {
        "once": "approved (this call only)",
        "sess": "approved (this session)",
        "always": "approved (always)",
        "deny": "denied",
    }.get(choice, "")
    # Popup feedback first — this works even if edit_message_text fails.
    try:
        await q.answer(text=label or choice)
    except Exception as e:
        log.warning("on_callback: q.answer raised: %r", e)
    try:
        await q.edit_message_text(
            (q.message.text or "(approval prompt)") + f"\n\n→ {label}"
        )
    except Exception as e:
        # v1.31.8 — log instead of silent swallow. The plan-review
        # message is plain-text (no parse_mode) but edit_message_text
        # defaults to whatever the original message had; if it tries
        # to re-parse markdown that's now-edited-with-arrow, we want
        # to know.
        log.warning("on_callback: edit_message_text raised: %r", e)


async def _handle_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """v1.31.8 — top-level Application error handler.

    Without this, python-telegram-bot's default behavior is to log
    handler exceptions via its own logger which (without basicConfig)
    sends them to nowhere visible. Field-validation finding from
    Sam's VPS: callbacks were arriving but on_callback was crashing
    silently, leaving the chat-turn approver hung on a future that
    never resolved. Adding an explicit error handler so any
    unhandled exception in any handler surfaces in the log.
    """
    err = ctx.error
    log.error(
        "telegram handler exception: %r | update=%r",
        err, getattr(update, "to_dict", lambda: update)(),
        exc_info=err,
    )


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

    # v1.31.17 — same stdout-buffering fix v1.31.15 applied to web.
    # Under ``nohup janus telegram > log 2>&1`` CPython block-buffers
    # stdout because it's a redirected file (not a TTY). The startup
    # banner + getUpdates trace lines never reach the log until the
    # buffer fills (~4-8KB), making "is this process actually running
    # and polling?" impossible to answer from `tail -5 log`.
    # systemd users don't see this (journalctl handles framing per
    # record), but Sam's VPS used nohup until v1.31.17 added a
    # janus-web.service unit. Belt-and-suspenders here too so non-
    # systemd setups keep working transparently.
    import sys as _sys
    try:
        _sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    config.assert_configured()
    config.ensure_home()

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        # v1.31.11 — enable concurrent update processing. Default in
        # PTB v20+ is sequential per-chat (preserves message order).
        # That serialization breaks the v1.31.10 text-reply fallback
        # because the user's "Y/N" message queues behind the still-
        # running on_text for the cost.py prompt — which can't
        # complete because the approver is blocked on a future the
        # text reply was supposed to resolve. Deadlock by design.
        # Enabling concurrent_updates(True) lets the second on_text
        # dispatch immediately, resolve the future, and unblock the
        # first. Order of unrelated messages within the same chat
        # is no longer strictly preserved — acceptable trade-off
        # since plan-mode approvals are interleaved with chat turns
        # by design and out-of-order delivery within an approval
        # window is the expected pattern.
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("logo", cmd_logo))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("sethome", cmd_sethome))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("swarm", cmd_swarm))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("interview", cmd_interview))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # v1.5.1: photo + document handlers so attachments don't silently disappear.
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_callback))

    # v1.31.8 — top-level error handler so handler exceptions surface
    # in the log instead of being silently dropped.
    app.add_error_handler(_handle_error)

    # v1.31.9 — catch-all update logger in a separate group so EVERY
    # update is logged regardless of whether other handlers match.
    # Group 99 means "after group 0" — group 0 handlers run first
    # (and stop dispatch within their group), then group 99 always
    # runs. This tells us definitively whether updates of any type
    # are reaching the dispatcher at all. Pairs with on_text /
    # on_callback FIRED logs to triangulate the field bug.
    from telegram.ext import TypeHandler
    from telegram import Update as _Upd

    async def _log_all_updates(update, ctx):
        try:
            kind = "?"
            if getattr(update, "callback_query", None):
                kind = "callback_query"
            elif getattr(update, "message", None):
                kind = "message"
            elif getattr(update, "edited_message", None):
                kind = "edited_message"
            chat_id = update.effective_chat.id if update.effective_chat else None
            log.info(
                "dispatcher saw update kind=%s chat=%s update_id=%s",
                kind, chat_id, getattr(update, "update_id", None),
            )
        except Exception as e:
            log.warning("catch-all logger raised: %r", e)

    app.add_handler(TypeHandler(_Upd, _log_all_updates), group=99)

    # v1.31.17 — flush=True belt-and-suspenders so the version banner
    # appears in nohup-redirected logs even if reconfigure failed.
    print(
        f"janus telegram gateway running ({branding.VERSION}). ctrl-c to stop.",
        flush=True,
    )
    log.info(
        "telegram gateway starting | log_level=%s | handlers=%d | "
        "set JANUS_TELEGRAM_LOG_LEVEL=INFO for handler-entry traces",
        _LOG_LEVEL,
        sum(len(h) for h in app.handlers.values()),
    )
    # v1.31.9 — log the handler types so we can verify
    # CallbackQueryHandler is actually registered.
    for group, handlers in app.handlers.items():
        for h in handlers:
            log.info(
                "registered handler: group=%s type=%s",
                group, type(h).__name__,
            )
    app.run_polling(allowed_updates=["message", "callback_query"])
