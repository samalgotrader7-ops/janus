"""
tools/a2a_call.py — model-callable A2A client (v1.40.2, Phase 10.4.2).

WHY:
v1.40.0 + v1.40.1 turned Janus into an A2A SERVER. This tool lets
Janus be an A2A CLIENT too — call into another agent that exposes
the same protocol. With both halves in place, Janus can both
delegate to and be delegated to by any spec-compliant peer.

USAGE FROM THE MODEL'S PERSPECTIVE:
    a2a_call(
      agent_url="https://other-agent.example.com",
      prompt="Help me classify these tickets",
      bearer_token="opt-secret",   # if remote auth requires it
      timeout=120,
    )

The tool:
  1. Fetches <agent_url>/.well-known/agent.json to confirm the
     remote is alive + grab its auth scheme.
  2. POSTs a JSON-RPC tasks/send to <agent_url>/a2a.
  3. Returns the agent's reply text (or an error message that
     names the failure mode without raising).

SAFETY:
  * dangerous=True — making outbound calls costs $$ (the remote
    agent runs an LLM turn) and reveals data to third parties.
  * risk='exec'
  * Capability ("a2a", "call", agent_url) — skills can pre-grant
    specific URLs.

NOT IN SCOPE FOR v1.40.2:
  * Async / streaming task lifecycle (waiting on input-required,
    polling) — tasks/send returns synchronously per v1.40.1 server
    impl. Other servers may run async; we'll add polling in
    v1.40.x if real-world peers need it.
  * mTLS / OAuth schemes beyond bearer.

NETWORK STACK:
We use stdlib urllib so we don't pull `requests` into core. Same
pattern as voice.py (v1.35.7).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import base


DEFAULT_TIMEOUT = 60
TIMEOUT_MAX = int(os.environ.get("JANUS_A2A_CLIENT_TIMEOUT", "300"))


def _fetch_json(url: str, *, headers: dict, body: bytes | None = None,
                timeout: int = DEFAULT_TIMEOUT) -> tuple[int, dict | None, str]:
    """GET (body=None) or POST (body!=None). Returns (status, json, text).

    Errors don't raise — they return (status, None, error_message).
    """
    method = "POST" if body is not None else "GET"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            return resp.status, data, text
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = str(e)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        return e.code, data, text
    except urllib.error.URLError as e:
        return 0, None, f"network error: {e.reason}"
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def _normalize_url(url: str) -> str:
    """Strip trailing slash so we can append /.well-known/... etc."""
    u = url.strip()
    while u.endswith("/"):
        u = u[:-1]
    return u


def _extract_artifact_text(task: dict) -> str:
    """Pull the most-recent text artifact's text out of a Task dict."""
    artifacts = task.get("artifacts") or []
    for art in reversed(artifacts):
        if not isinstance(art, dict):
            continue
        for p in art.get("parts") or []:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and p.get("text"):
                return str(p["text"])
            if "text" in p and p.get("text"):
                return str(p["text"])
    # Fallback: try the latest message
    msg = task.get("message")
    if isinstance(msg, dict):
        for p in msg.get("parts") or []:
            if isinstance(p, dict) and p.get("text"):
                return str(p["text"])
    return ""


class A2ACall(base.Tool):
    name = "a2a_call"
    description = (
        "Call another A2A-compliant agent. Discovers the remote at "
        "<agent_url>/.well-known/agent.json, submits a tasks/send "
        "JSON-RPC request to <agent_url>/a2a, returns the remote "
        "agent's reply text. Use this when you want to delegate a "
        "self-contained sub-task to a peer agent — the remote has "
        "ZERO context from this conversation, so write a complete "
        "brief. Pass bearer_token if the remote's Agent Card "
        "declares 'bearer' auth. Timeouts are wall-clock (default "
        "60s, capped at JANUS_A2A_CLIENT_TIMEOUT/300s). DESTRUCTIVE "
        "— calls cost money on the remote side and may exfiltrate "
        "the prompt to a third party."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_url": {
                "type": "string",
                "description": (
                    "Base URL of the remote A2A agent. e.g. "
                    "'https://other-agent.example.com'. The tool "
                    "appends /.well-known/agent.json (discovery) "
                    "and /a2a (JSON-RPC tasks endpoint)."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The instruction to send. Plain text — the "
                    "remote agent has no other context."
                ),
            },
            "bearer_token": {
                "type": "string",
                "description": (
                    "Bearer token for the remote's Authorization "
                    "header. Required if its Agent Card declares "
                    "'bearer' authentication. Omit for 'none'."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Wall-clock timeout in seconds. Default "
                    f"{DEFAULT_TIMEOUT}, hard-cap "
                    f"JANUS_A2A_CLIENT_TIMEOUT (default {TIMEOUT_MAX}s)."
                ),
            },
        },
        "required": ["agent_url", "prompt"],
    }
    dangerous = True
    risk = "exec"

    def run(self, args: dict, approver: base.Approver) -> str:
        agent_url = (args.get("agent_url") or "").strip()
        if not agent_url:
            return "a2a_call: agent_url required"
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "a2a_call: empty prompt"

        # Sanity: must be http/https
        parsed = urllib.parse.urlparse(agent_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return f"a2a_call: agent_url must be an http(s) URL: {agent_url}"

        agent_url = _normalize_url(agent_url)
        bearer = (args.get("bearer_token") or "").strip()

        try:
            timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
        except (ValueError, TypeError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, min(timeout, TIMEOUT_MAX))

        # Approval — capability lets a skill pre-grant per agent_url.
        details = (
            f"agent_url: {agent_url}\n"
            f"prompt:    {prompt[:200]}{'…' if len(prompt) > 200 else ''}\n"
            f"timeout:   {timeout}s   bearer: {'set' if bearer else 'none'}"
        )
        ok = approver(
            "a2a.call",
            details,
            capability=("a2a", "call", agent_url),
        )
        if not ok:
            return "a2a_call: refused by user."

        # ---------- Step 1: discovery ----------
        card_url = f"{agent_url}/.well-known/agent.json"
        status, card, text = _fetch_json(
            card_url, headers={"Accept": "application/json"}, timeout=timeout,
        )
        if status == 0:
            return f"a2a_call: discovery failed: {text}"
        if status >= 400:
            return f"a2a_call: discovery returned HTTP {status}: {text[:300]}"
        if not isinstance(card, dict):
            return f"a2a_call: agent.json was not a JSON object: {text[:200]}"

        # ---------- Step 2: tasks/send ----------
        envelope = {
            "jsonrpc": "2.0",
            "method": "tasks/send",
            "params": {
                "id": uuid.uuid4().hex,
                "sessionId": uuid.uuid4().hex,
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": prompt}],
                },
            },
            "id": 1,
        }
        body = json.dumps(envelope).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        status, resp, text = _fetch_json(
            f"{agent_url}/a2a",
            headers=headers,
            body=body,
            timeout=timeout,
        )
        if status == 0:
            return f"a2a_call: network error: {text}"
        if status == 401:
            return (
                "a2a_call: remote rejected auth (401). The remote's Agent Card "
                f"declares schemes={card.get('authentication', {}).get('schemes', [])}; "
                "pass bearer_token if 'bearer' is required."
            )
        if status >= 400:
            return f"a2a_call: tasks/send returned HTTP {status}: {text[:300]}"
        if not isinstance(resp, dict):
            return f"a2a_call: malformed response: {text[:200]}"

        if "error" in resp:
            err = resp["error"] if isinstance(resp["error"], dict) else {}
            return (
                f"a2a_call: remote JSON-RPC error "
                f"{err.get('code', '?')}: {err.get('message', text[:200])}"
            )

        result = resp.get("result")
        if not isinstance(result, dict):
            return f"a2a_call: unexpected response shape: {text[:200]}"

        state = result.get("state", "?")
        artifact_text = _extract_artifact_text(result)
        if state == "completed" and artifact_text:
            return artifact_text
        if state == "completed":
            return "a2a_call: completed (no text artifact returned)"
        if state in ("failed", "canceled"):
            return f"a2a_call: remote task {state}: {artifact_text or '(no detail)'}"
        return (
            f"a2a_call: task in non-terminal state '{state}'. "
            f"Async / input-required handling lands in v1.40.x. "
            f"Task id: {result.get('id', '?')}"
        )
