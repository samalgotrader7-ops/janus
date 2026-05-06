"""
gateways/web_audit.py — append-only audit log for the web gateway.

Records every authentication event and every state-changing API call to
~/.janus/web_audit.jsonl. Plain-text JSONL (P5): `tail -f`, `grep`, or
process via `jq` like any other log.

EVENTS RECORDED:
- login.success   { ip, ua, ts }
- login.failure   { ip, ua, ts, reason }     # bad token / IP blocked
- session.create  { sid, ip, ts }
- session.destroy { sid, ip, ts, reason }    # logout / expired / invalidated
- token.rotate    { ip, ts }                 # bootstrap token replaced
- chat            { sid, ip, ts, request_len }
- mutate          { sid, ip, ts, route, body_keys }   # POST /home etc.
- rate_limited    { sid, ip, ts, route_class, retry_after }

REDACTION:
Tokens, secrets, and user request content are NEVER logged in full —
only lengths, route, status, and metadata. The user can correlate with
the main `~/.janus/log.jsonl` if they need the request body.

THREAD SAFETY:
Append-only writes are atomic up to the OS line buffer. Multiple
gateway workers can write concurrently without corrupting lines.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from .. import config


_LOCK = threading.Lock()


def _audit_path():
    return config.HOME / "web_audit.jsonl"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write(event: str, **fields: Any) -> None:
    """Append one audit event. Failure-silent — auditing must never
    break the request path."""
    record = {"ts": _now_iso(), "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False, default=str)
    p = _audit_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        # Disk full / permission denied — best-effort. Don't crash auth.
        pass


def login_success(ip: str, ua: str = "") -> None:
    write("login.success", ip=ip, ua=ua[:200])


def login_failure(ip: str, reason: str, ua: str = "") -> None:
    write("login.failure", ip=ip, reason=reason, ua=ua[:200])


def session_create(sid: str, ip: str) -> None:
    write("session.create", sid=sid, ip=ip)


def session_destroy(sid: str, ip: str, reason: str = "") -> None:
    write("session.destroy", sid=sid, ip=ip, reason=reason)


def token_rotate(ip: str = "") -> None:
    write("token.rotate", ip=ip)


def chat(sid: str, ip: str, request_len: int) -> None:
    write("chat", sid=sid, ip=ip, request_len=request_len)


def mutate(sid: str, ip: str, route: str, body_keys: list[str]) -> None:
    write("mutate", sid=sid, ip=ip, route=route, body_keys=body_keys)


def rate_limited(
    sid: str, ip: str, route_class: str, retry_after: float,
) -> None:
    write(
        "rate_limited", sid=sid, ip=ip,
        route_class=route_class, retry_after=round(retry_after, 2),
    )


def csrf_failure(sid: str, ip: str, route: str) -> None:
    write("csrf.failure", sid=sid, ip=ip, route=route)
