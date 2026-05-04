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

from .. import config, executor, logger, memory, skills, permissions, cost
from ..tools import default_registry, make_protected, CapabilitySet
from . import _common as gw

GATEWAY_NAME = "whatsapp"

# Per-sender conversation state cache (backed by gw.GatewaySession on disk).
_SESSIONS: dict[str, list[dict]] = {}


def _load_messages(sender: str) -> list[dict]:
    if sender in _SESSIONS:
        return _SESSIONS[sender]
    persisted = gw.load_session(GATEWAY_NAME, sender)
    _SESSIONS[sender] = persisted.messages
    return _SESSIONS[sender]


def _persist_messages(sender: str, messages: list[dict], mode: str) -> None:
    sess = gw.load_session(GATEWAY_NAME, sender)
    sess.messages = messages
    sess.mode = mode
    gw.save_session(sess)


def _is_authorized(sender: str) -> bool:
    return gw.is_authorized(
        GATEWAY_NAME, sender,
        env_allowlist=config.WHATSAPP_ALLOWED_NUMBERS or "",
    )


def _greeted(sender: str) -> bool:
    s = gw.load_session(GATEWAY_NAME, sender)
    return bool(s.extras.get("greeted"))


def _mark_greeted(sender: str) -> None:
    s = gw.load_session(GATEWAY_NAME, sender)
    s.extras["greeted"] = True
    gw.save_session(s)


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


def _make_whatsapp_approver(mode: str):
    """Mode-aware approver. ASK becomes DENY because there's no inline
    approval UI on WhatsApp. Use acceptEdits/bypassPermissions via
    JANUS_APPROVAL or attach a skill with capability tokens."""
    def approver(action_label: str, details: str, **kw) -> bool:
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        decision = permissions.decide(risk, mode)
        if decision == permissions.ALLOW:
            return True
        return False
    return approver


def _handle_inbound(msg: dict) -> str | None:
    """v1.3 chat-shaped handler with pairing, slash commands, self-intro."""
    sender = msg.get("from", "")
    text = msg.get("text", "")
    if not text:
        return None

    # v1.3 pairing — unrecognized numbers receive a code instead of a chat.
    if not _is_authorized(sender):
        code = gw.request_pairing(GATEWAY_NAME, sender, user_label=sender)
        return (
            f"Hi! I don't recognize this number yet.\n\n"
            f"Pairing code: {code}\n\n"
            f"Ask the bot owner to run:\n"
            f"janus pair approve {code}\n\n"
            f"Once approved, send any message and I'll respond."
        )

    # v1.3 slash commands — minimal surface (WhatsApp doesn't have native UI).
    s = text.strip()
    if s.startswith("/"):
        return _handle_command(sender, s)

    # v1.3 self-intro on first authorized text.
    intro = ""
    if not _greeted(sender):
        intro = gw.greeting() + "\n\n"
        _mark_greeted(sender)
        # Pure greeting? The intro IS the reply.
        if s.lower().strip(" .!,?") in ("hi", "hello", "hey", "yo", "sup"):
            return intro.strip()

    messages = _load_messages(sender)
    mode = permissions.normalize(config.APPROVAL_MODE)
    base_approver = _make_whatsapp_approver(mode)
    caps = CapabilitySet()
    tools = default_registry(capabilities=caps)
    approver = make_protected(base_approver, caps, mode)
    preamble = memory.prepend_for_prompt()

    record: dict[str, Any] = {
        "ts": logger.now_iso(),
        "model": config.MODEL,
        "workspace": str(config.WORKSPACE),
        "request": text,
        "gateway": GATEWAY_NAME,
        "sender": sender,
        "mode": mode,
    }
    try:
        output, trace = executor.chat(
            messages=messages,
            user_input=text,
            tools=tools,
            approver=approver,
            memory_preamble=preamble,
            mode=mode,
            workspace=str(config.WORKSPACE),
            tool_count=len(tools.names()),
            skill_count=len(skills.list_skills()),
            stream=False,
        )
        record["output"] = output
        record["trace"] = trace
        try:
            _persist_messages(sender, messages, mode)
        except Exception:
            pass
        # v1.3 L3 #2 — per-chat cost ledger.
        try:
            ts = cost.turn_stats()
            cost.record_per_chat(
                gateway=GATEWAY_NAME, chat_id=sender,
                identity=gw.identity_for(GATEWAY_NAME, sender) or "",
                model=config.MODEL,
                prompt_tokens=ts.prompt_tokens,
                completion_tokens=ts.completion_tokens, usd=ts.usd,
            )
        except Exception:
            pass
    except Exception as e:
        record["error"] = f"execute: {e}"
        output = f"executor error: {e}"
    logger.write(record)
    return (intro + output) if intro else output


def _handle_command(sender: str, line: str) -> str:
    """Minimal WhatsApp command surface (v1.3)."""
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/sethome":
        gw.set_home(GATEWAY_NAME, sender)
        return f"✅ Home channel set to {sender}.\nCron and cross-platform messages will be delivered here."
    if cmd == "/memory":
        if arg:
            body = memory.read(arg)
            return body.strip() or f"(no {arg}.md yet)"
        cats = memory.list_categories()
        if not cats:
            return ("(no memory yet — categories ready: "
                    f"{', '.join(config.MEMORY_CATEGORIES)})")
        return "\n\n".join(
            f"━ {c}.md ━\n{memory.read(c).strip()}" for c in cats
        )
    if cmd == "/skills":
        items = skills.list_skills()
        if not items:
            return "no skills installed."
        lines = [f"• {s.name} ({s.state}) — {s.description}" for s in items[:30]]
        if len(items) > 30:
            lines.append(f"... ({len(items) - 30} more)")
        return "\n".join(lines)
    if cmd == "/swarm":
        from .. import swarms as _swarms
        return _swarms.slash.handle(arg)
    if cmd == "/clear":
        _SESSIONS[sender] = []
        try:
            sess = gw.load_session(GATEWAY_NAME, sender)
            sess.messages = []
            gw.save_session(sess)
        except Exception:
            pass
        return "conversation cleared."
    if cmd == "/cost":
        identity = gw.identity_for(GATEWAY_NAME, sender) or ""
        return cost.render_per_chat(GATEWAY_NAME, sender, identity)
    if cmd in ("/help", "/?"):
        return (
            "commands: /sethome /memory [cat] /skills /swarm /cost /clear /help\n\n"
            "type any other text to chat."
        )
    return f"unknown command: {cmd}\ntry /help"


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
