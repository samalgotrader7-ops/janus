"""
tools/telegram_react.py — react to the user's last Telegram message (v1.18.2).

The Telegram Bot API supports message reactions (since Bot API 7.0):
``setMessageReaction(chat_id, message_id, reaction=[{"type": "emoji",
"emoji": "👍"}])``. This tool exposes that to the model so Janus can
acknowledge a greeting, say "got it" without sending a full reply, or
just feel less robotic in chat.

The tool is GATEWAY-BOUND — instances of this class are created by the
Telegram gateway with a closure over (chat_id, message_id_provider) so
the model only needs to pass the emoji. Outside the Telegram gateway
(CLI / headless / sub-agent) the tool returns a "not in Telegram" error.

P8: errors are observations. The model reads "(reaction skipped: not
on Telegram)" and adapts.

WHY a closure-based tool, not a chat_id+message_id parameter:
The model would otherwise need to track the most-recent user message
ID itself, which is fiddly and error-prone. The gateway already knows
which chat it's running in and the most-recent inbound message_id, so
it injects them via closure — same pattern as gateway_send_file.

ALLOWED EMOJI:
Telegram restricts reactions to a set of standard emojis. The full list
varies by chat type / bot. We don't enforce — Telegram returns
BAD_REQUEST for invalid emojis and we surface that as the error string.
Common ones that almost always work: 👍 ❤️ 🔥 🎉 😁 🤔 😢 👀 🙏 💯
"""

from __future__ import annotations
from typing import Callable

import requests

from .. import config
from .base import Tool


_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramReact(Tool):
    """React to the user's most recent Telegram message with an emoji.

    Constructor takes a callback (() -> tuple[chat_id, message_id]) that
    returns the current chat's most-recent inbound message — typically
    a closure into the gateway's per-chat session state.

    When constructed without a callback (the default registry version),
    .run() returns a "not on Telegram" error so non-gateway contexts
    don't crash.
    """

    name = "telegram_react"
    description = (
        "React to the user's most recent message in this Telegram chat "
        "with a single emoji. Use for friendliness — acknowledge a "
        "greeting (👍), confirm receipt (✅), show enthusiasm (🔥) "
        "without writing a full reply. Lightweight social signal, not "
        "a substitute for actually answering. Telegram-only — outside "
        "the Telegram gateway this returns an error."
    )
    parameters = {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": (
                    "Single emoji from Telegram's standard set. "
                    "Common safe choices: 👍 ❤️ 🔥 🎉 😁 🤔 😢 👀 🙏 💯 "
                    "✅ 👏 🤝 ⚡ 🥰 🤯"
                ),
            },
        },
        "required": ["emoji"],
    }
    risk = "read"  # social signal, no state mutation in any meaningful sense
    dangerous = False

    def __init__(
        self,
        msg_id_callback: Callable[[], tuple[int | str, int] | None] | None = None,
    ):
        self._msg_id_cb = msg_id_callback

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        emoji = (args.get("emoji") or "").strip()
        if not emoji:
            return "error: emoji is required"
        if self._msg_id_cb is None:
            return (
                "(reaction skipped: not in a Telegram gateway — "
                "telegram_react needs the gateway's chat_id + message_id "
                "closure, only set when running inside the bot)"
            )
        target = self._msg_id_cb()
        if not target:
            return (
                "(reaction skipped: no recent inbound message to react "
                "to — wait for the user to message you)"
            )
        chat_id, message_id = target
        token = config.TELEGRAM_BOT_TOKEN or None
        if not token:
            return "error: JANUS_TELEGRAM_TOKEN is not set"
        url = _API_BASE.format(token=token, method="setMessageReaction")
        payload = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "reaction": [{"type": "emoji", "emoji": emoji}],
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
        except requests.RequestException as e:
            return f"error: network: {type(e).__name__}: {e}"
        if resp.status_code != 200:
            return (
                f"error: Telegram API returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        return f"reacted with {emoji}"
