"""
tools/gateway_send_file.py — send a local file to the user via the
active gateway (Telegram, etc.). v1.5.1 phase 5.

Bug it solves:
User asked the Telegram bot "send me that MD file to the telegram".
The bot replied "Here's the full content you asked for. You can copy
and paste it…" and pasted the entire 11KB markdown content into the
chat as a text message. That's not "sending the file" — that's
"showing the contents". The model defaulted to fs_read + paste because
no tool existed for "send this file as a Telegram document attachment".

This module fixes that: a tool the gateway registers with a `send_fn`
closure over the bot + chat_id. When the model calls
gateway_send_file(path="..."), the closure fires
bot.send_document(chat_id, open(path, "rb"), caption=...) — Telegram
delivers the file as a downloadable attachment.

Outside a gateway (CLI, headless), the tool returns an error string
explaining it's gateway-only. The model can adapt — fs_read or
similar — without crashing.

Each gateway is responsible for constructing this tool with its own
send_fn and adding it to the Registry. CLI and headless surfaces don't
register it; the model still sees fs_read for those cases.
"""

from __future__ import annotations
from pathlib import Path
from typing import Callable

from .base import Tool


class GatewaySendFile(Tool):
    """Send a local file via the active gateway. Constructed by the
    gateway per chat turn with `send_fn` bound to bot+chat_id."""
    name = "gateway_send_file"
    description = (
        "Send a LOCAL file to the user as an attachment via the active "
        "gateway (Telegram, etc.). Use this when the user says \"send me "
        "the file\", \"forward me the .md\", \"upload it\", or similar. "
        "Do NOT use fs_read + paste content for this — paste-the-content "
        "is not the same as sending the actual file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute or relative path to a local file to send. "
                    "Must exist and be readable."
                ),
            },
            "caption": {
                "type": "string",
                "description": (
                    "Optional caption text to display alongside the file "
                    "in the chat. Keep short."
                ),
            },
        },
        "required": ["path"],
    }
    risk = "exec"

    def __init__(self, send_fn: Callable[[str, str], None] | None = None):
        # send_fn(path, caption) — gateway injects this. None = not
        # in a gateway context, tool returns explanatory error.
        self._send_fn = send_fn

    def run(self, args: dict, approver) -> str:
        if self._send_fn is None:
            return (
                "error: gateway_send_file only works when running inside "
                "a gateway (telegram, web, whatsapp). On CLI / headless "
                "you can fs_read the file or print its path."
            )
        path = args.get("path", "").strip()
        caption = args.get("caption", "")
        if not path:
            return "error: path required"
        p = Path(path).expanduser()
        if not p.is_file():
            return f"error: not a file: {p}"

        if not approver(
            f"send file via gateway",
            f"{p.name} ({p.stat().st_size} bytes)",
            capability=("gateway", "send", str(p)),
        ):
            return f"refused: gateway_send_file({p.name})"

        try:
            self._send_fn(str(p), caption)
        except Exception as e:
            return f"error: send failed: {type(e).__name__}: {e}"
        return f"sent {p.name} ({p.stat().st_size} bytes) via gateway"
