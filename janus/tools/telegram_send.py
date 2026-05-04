"""
tools/telegram_send.py — direct Telegram Bot API tools (v1.5.2).

Two tools that work from ANY context (CLI, headless, gateway, swarm
sub-agents) as long as JANUS_TELEGRAM_TOKEN is set:

  telegram_send_file(path, chat_id, caption?)
    Sends a local file as an attachment via bot.sendDocument.

  telegram_send_message(text, chat_id, parse_mode?)
    Sends a text message via bot.sendMessage.

WHY THESE EXIST (vs. gateway_send_file):
  gateway_send_file uses a closure over the gateway's bot+chat_id, so
  it ONLY works when the model is running INSIDE the gateway's chat
  loop. From CLI, the user can ask "send the report to my telegram"
  and these tools handle it — the model passes a chat_id (looked up
  from session history or asked from the user) and the tool POSTs to
  Telegram's HTTP API directly.

P6: pure `requests`, no python-telegram-bot import. Same call path as
janus.llm — keeps the dependency surface flat.

P8: HTTP errors are observations the model reads (e.g., "error: chat_id
not found"). Tool never raises.
"""

from __future__ import annotations
from pathlib import Path

import requests

from .. import config
from .base import Tool


_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _token_or_none() -> str | None:
    return config.TELEGRAM_BOT_TOKEN or None


# ---------- telegram_send_file ----------


class TelegramSendFile(Tool):
    """Send a local file to a Telegram chat as an attachment.

    Works from CLI, headless, gateway — anywhere JANUS_TELEGRAM_TOKEN
    is configured. Use this when the user says "send the file to
    telegram" / "DM me the report" / similar."""

    name = "telegram_send_file"
    description = (
        "Send a LOCAL file as an attachment to a Telegram chat via "
        "the Telegram Bot API. Works from CLI / headless / gateway "
        "contexts (anywhere JANUS_TELEGRAM_TOKEN is set). Use this "
        "when the user asks to send / forward / DM a file via "
        "Telegram. Look up chat_id from session_recent if not "
        "provided. NOT to be confused with gateway_send_file, which "
        "only works inside the Telegram gateway chat loop."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative local file path to send.",
            },
            "chat_id": {
                "type": "string",
                "description": (
                    "Telegram chat_id (numeric, e.g. '123456789'). "
                    "Look it up via session_recent if you don't have "
                    "it — recent Telegram interactions log the chat_id."
                ),
            },
            "caption": {
                "type": "string",
                "description": "Optional caption text alongside the file.",
            },
        },
        "required": ["path", "chat_id"],
    }
    risk = "exec"

    def run(self, args: dict, approver) -> str:
        token = _token_or_none()
        if not token:
            return (
                "error: JANUS_TELEGRAM_TOKEN is not set. Configure the "
                "token in ~/.janus/.env or env to enable Telegram sends."
            )
        path = (args.get("path") or "").strip()
        chat_id = str(args.get("chat_id") or "").strip()
        caption = args.get("caption", "")
        if not path:
            return "error: path required"
        if not chat_id:
            return "error: chat_id required (look it up via session_recent)"
        p = Path(path).expanduser()
        if not p.is_file():
            return f"error: not a file: {p}"

        if not approver(
            f"telegram_send_file → {chat_id}",
            f"{p.name} ({p.stat().st_size} bytes)",
            capability=("telegram", "send_file", chat_id),
        ):
            return f"refused: telegram_send_file({p.name} → {chat_id})"

        url = _API_BASE.format(token=token, method="sendDocument")
        try:
            with open(p, "rb") as fh:
                resp = requests.post(
                    url,
                    data={
                        "chat_id": chat_id,
                        "caption": (caption or "")[:1024],
                    },
                    files={"document": (p.name, fh)},
                    timeout=60,
                )
        except requests.RequestException as e:
            return f"error: network: {type(e).__name__}: {e}"
        if resp.status_code != 200:
            return (
                f"error: Telegram API returned {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        return f"sent {p.name} ({p.stat().st_size} bytes) to chat {chat_id}"


# ---------- telegram_send_message ----------


class TelegramSendMessage(Tool):
    """Send a text message to a Telegram chat via the Bot API.

    Use sparingly — most chat-shaped surfaces already deliver the model's
    output to the user as the assistant message. This is for OUT-OF-BAND
    sends: notify a different chat, send a reminder, etc."""

    name = "telegram_send_message"
    description = (
        "Send a text message to a Telegram chat via the Bot API. "
        "Use for OUT-OF-BAND notifications (a different chat than "
        "the current conversation). Don't use this to reply to the "
        "user in the current chat — the framework already delivers "
        "your assistant message there."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Message body. Markdown supported by default.",
            },
            "chat_id": {
                "type": "string",
                "description": "Telegram chat_id to send to.",
            },
            "parse_mode": {
                "type": "string",
                "description": (
                    "'Markdown' or 'HTML'. Empty for plain text. "
                    "Default: Markdown."
                ),
            },
        },
        "required": ["text", "chat_id"],
    }
    risk = "exec"

    def run(self, args: dict, approver) -> str:
        token = _token_or_none()
        if not token:
            return (
                "error: JANUS_TELEGRAM_TOKEN is not set. Configure the "
                "token in ~/.janus/.env or env to enable Telegram sends."
            )
        text = (args.get("text") or "").strip()
        chat_id = str(args.get("chat_id") or "").strip()
        parse_mode = args.get("parse_mode", "Markdown")
        if not text:
            return "error: text required"
        if not chat_id:
            return "error: chat_id required"

        if not approver(
            f"telegram_send_message → {chat_id}",
            text[:120],
            capability=("telegram", "send_message", chat_id),
        ):
            return f"refused: telegram_send_message → {chat_id}"

        url = _API_BASE.format(token=token, method="sendMessage")
        payload: dict = {"chat_id": chat_id, "text": text[:4096]}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            return f"error: network: {type(e).__name__}: {e}"
        if resp.status_code != 200:
            return (
                f"error: Telegram API returned {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        return f"sent message ({len(text)} chars) to chat {chat_id}"
