"""
gateways/whatsapp.py — Phase 11: WhatsApp Cloud API gateway (optional).

WHY:
WhatsApp reach without leaving Janus's safety thesis. Same interpreter +
executor; per-chat allowlist via JANUS_WHATSAPP_ALLOWED.

REQUIREMENTS (env):
- JANUS_WHATSAPP_TOKEN         — Meta Cloud API access token
- JANUS_WHATSAPP_PHONE_ID      — sender phone-number ID
- JANUS_WHATSAPP_VERIFY        — webhook verify token (you pick a string)
- JANUS_WHATSAPP_ALLOWED       — comma-separated allowed phone numbers
                                 (recipients you'll respond to)

If any required env var is missing, `serve()` exits with a clear error
instead of stack-tracing. The module imports cleanly without them; only
serve() asserts.

OPTIONAL DEPENDENCY:
None — pure stdlib (`http.server`) + `requests`. We don't pull a Meta
SDK in (P6).

WHAT'S HERE (v1):
- Webhook verifier (GET /whatsapp/webhook).
- Inbound message parser (POST /whatsapp/webhook).
- Outbound `send_message(to, text)` via Meta REST API.
- Per-message allowlist check.

WHAT'S NOT HERE:
- Media (image/video/audio) inbound handling.
- Status callbacks (delivery/read receipts).
- Multi-tenant routing.
"""

from __future__ import annotations
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests

from .. import config, interpreter, executor, logger, memory
from ..tools import default_registry, make_capability_aware, CapabilitySet


_GRAPH_URL = "https://graph.facebook.com/v20.0"


def _allowed_numbers() -> set[str]:
    raw = (config.WHATSAPP_ALLOWED_NUMBERS or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def send_message(to: str, text: str) -> dict:
    """Send a text message via Meta Cloud API. Returns the API response dict
    (or `{"error": ...}` on failure — never raises)."""
    if not config.WHATSAPP_TOKEN or not config.WHATSAPP_PHONE_ID:
        return {"error": "JANUS_WHATSAPP_TOKEN / JANUS_WHATSAPP_PHONE_ID unset"}
    url = f"{_GRAPH_URL}/{config.WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4096]},  # Meta cap
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code >= 400:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def parse_inbound(body: dict) -> list[dict]:
    """Extract {from, text} dicts from an inbound webhook payload.

    Meta's payload is deeply nested. We walk to entry[].changes[].value.messages[].
    """
    out: list[dict] = []
    entries = body.get("entry") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return out
    for entry in entries:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            value = change.get("value") if isinstance(change, dict) else {}
            for msg in (value.get("messages") or []) if isinstance(value, dict) else []:
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") != "text":
                    continue
                text_block = msg.get("text") or {}
                out.append({
                    "from": str(msg.get("from") or ""),
                    "text": str(text_block.get("body") or "").strip(),
                    "id": str(msg.get("id") or ""),
                })
    return out


def _handle_inbound(msg: dict) -> str | None:
    """Run interpreter+executor for one inbound message. Returns the reply
    text (or None to swallow)."""
    sender = msg.get("from", "")
    text = msg.get("text", "")
    allow = _allowed_numbers()
    if allow and sender not in allow:
        return None  # silently drop
    if not text:
        return None

    preamble = memory.prepend_for_prompt()
    try:
        interps = interpreter.interpret(
            text, memory_preamble=preamble, skill_hints="",
        )
    except Exception as e:
        return f"interpreter error: {e}"

    chosen = interps[0] if interps else {"label": "raw", "action": text, "risk": ""}

    def deny_approver(*a, **kw):
        return False  # gateway has no TTY for approval

    caps = CapabilitySet()
    tools = default_registry(capabilities=caps)
    approver = make_capability_aware(deny_approver, caps)

    record: dict[str, Any] = {
        "ts": logger.now_iso(), "model": config.MODEL,
        "workspace": str(config.WORKSPACE), "request": text,
        "gateway": "whatsapp", "sender": sender,
        "interpretations": interps, "choice": "auto-first",
    }
    try:
        output, trace = executor.execute(
            original_request=text,
            chosen_label=chosen["label"],
            chosen_action=chosen["action"],
            tools=tools,
            approver=approver,
            memory_preamble=preamble,
        )
        record["output"] = output
        record["trace"] = trace
    except Exception as e:
        record["error"] = f"execute: {e}"
        output = f"executor error: {e}"
    logger.write(record)
    return output


# ---------- HTTP server ----------


class _WhatsAppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(
            "whatsapp: " + (fmt % args if args else fmt) + "\n",
        )

    def do_GET(self):
        if self.path.split("?")[0] != "/whatsapp/webhook":
            return self._send(404, "not found")
        # Meta verification handshake.
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        mode = (q.get("hub.mode") or [""])[0]
        token = (q.get("hub.verify_token") or [""])[0]
        challenge = (q.get("hub.challenge") or [""])[0]
        if mode == "subscribe" and token == config.WHATSAPP_VERIFY_TOKEN:
            return self._send(200, challenge)
        return self._send(403, "forbidden")

    def do_POST(self):
        if self.path.split("?")[0] != "/whatsapp/webhook":
            return self._send(404, "not found")
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._send(400, "bad json")
        for msg in parse_inbound(data):
            try:
                reply = _handle_inbound(msg)
            except Exception as e:
                reply = f"error: {e}"
            if reply:
                send_message(msg["from"], reply)
        return self._send(200, "ok")

    def _send(self, code: int, body: str) -> None:
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def serve(host: str = "127.0.0.1", port: int = 8766) -> int:
    if not config.WHATSAPP_TOKEN or not config.WHATSAPP_PHONE_ID:
        print("error: JANUS_WHATSAPP_TOKEN and JANUS_WHATSAPP_PHONE_ID required")
        return 1
    if not config.WHATSAPP_VERIFY_TOKEN:
        print("error: JANUS_WHATSAPP_VERIFY required (string of your choosing)")
        return 1
    config.assert_configured()
    config.ensure_home()
    print(f"janus whatsapp webhook on http://{host}:{port}/whatsapp/webhook")
    server = ThreadingHTTPServer((host, port), _WhatsAppHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
