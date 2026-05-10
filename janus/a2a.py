"""
a2a.py — Agent-to-Agent Protocol foundations (v1.40.0,
Phase 10.4.0).

WHY:
Sam's 4-ideas brief Layer B: speak A2A so Janus can be discovered
by, and call into, other agents. A2A (Linux Foundation) is the
emerging cross-vendor agent-interop standard. The first piece is
the Agent Card — a JSON document at ``/.well-known/agent.json``
that declares an agent's identity, skills, authentication, and
endpoint URL.

This module ships ONLY the Agent Card builder. v1.40.1 adds the
``/a2a/tasks`` JSON-RPC endpoint (server side); v1.40.2 adds the
``a2a_call`` client tool so Janus can call other A2A agents.

DESIGN (locked with Sam, 2026-05-10):
  * Bolt onto `janus web` (shares the FastAPI app)
  * Bearer-token auth first; mTLS later
  * Agent Card is PUBLIC (no auth on /.well-known/agent.json) —
    it's the discovery endpoint, by spec

ENV CONFIG:
  JANUS_A2A_NAME         displayed name (default 'Janus')
  JANUS_A2A_URL          public URL where /a2a/tasks is reachable.
                         REQUIRED if you want other agents to call
                         back. Without it, the card lists "" and
                         clients cannot dial.
  JANUS_A2A_DESCRIPTION  human description (default branding.TAGLINE)
  JANUS_A2A_AUTH         'bearer' (default) | 'none'
  JANUS_A2A_PROVIDER     organization name (default 'Janus deployment')

SKILLS DECLARED:
We surface a small, stable list of A2A skill descriptors derived
from Janus's tool registry — enough for the client side to pick
Janus for the right kind of work. We DON'T enumerate every tool —
that's noise. The list groups tools into a few coarse skills.
"""

from __future__ import annotations

import os
from typing import Any

from . import branding


# Coarse-grained skills — what Janus offers as an A2A peer. Tags
# help requesters' agent-routers match capability to need.
DEFAULT_SKILLS: list[dict] = [
    {
        "id": "code-edit",
        "name": "Code reading and editing",
        "description": (
            "Read, edit, and create files; multi-file refactors with "
            "diff review; codebase navigation via grep/glob."
        ),
        "tags": ["code", "files", "edit", "refactor"],
    },
    {
        "id": "shell-exec",
        "name": "Shell command execution",
        "description": (
            "Run shell commands inside a workspace with approval "
            "gates. Includes background jobs and PTY for "
            "interactive tools."
        ),
        "tags": ["shell", "exec", "build", "test"],
    },
    {
        "id": "research",
        "name": "Web + memory search",
        "description": (
            "Fetch URLs, search the web, search the agent's "
            "structured memory store of past interactions."
        ),
        "tags": ["search", "web", "research", "memory"],
    },
    {
        "id": "delegation",
        "name": "Sub-agent delegation",
        "description": (
            "Spawn focused subagents (via Janus's subagent tool) "
            "or hand off to external CLIs (Claude Code, Aider, "
            "Codex, Gemini) when Janus is the orchestrator."
        ),
        "tags": ["orchestration", "subagent", "external-cli"],
    },
    {
        "id": "memory-and-skills",
        "name": "Persistent memory + self-improving skills",
        "description": (
            "Plain-text memory cards across conversations; skills "
            "that evolve via a learning loop."
        ),
        "tags": ["memory", "skills", "learning-loop"],
    },
]


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key, "").strip()
    return v if v else default


def build_agent_card() -> dict[str, Any]:
    """Return the JSON-serializable Agent Card.

    Per the A2A spec, this is what /.well-known/agent.json should
    return as application/json. Future spec revs may add fields;
    we keep the existing keys stable and extend additively.
    """
    name = _env("JANUS_A2A_NAME", "Janus")
    description = _env(
        "JANUS_A2A_DESCRIPTION",
        branding.TAGLINE or "Self-improving AI agent for developers.",
    )
    url = _env("JANUS_A2A_URL", "")
    provider = _env("JANUS_A2A_PROVIDER", "Janus deployment")

    auth_mode = _env("JANUS_A2A_AUTH", "bearer").lower()
    if auth_mode not in ("bearer", "none"):
        auth_mode = "bearer"
    auth_block: dict[str, Any]
    if auth_mode == "none":
        auth_block = {"schemes": []}
    else:
        auth_block = {"schemes": ["bearer"]}

    return {
        "name": name,
        "description": description,
        "url": url,
        "version": branding.VERSION,
        "provider": {"organization": provider},
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": list(DEFAULT_SKILLS),
        "authentication": auth_block,
    }


def auth_required() -> bool:
    """True if /a2a/* endpoints (v1.40.1+) should enforce bearer auth.

    Centralized here so v1.40.1's tasks endpoint reads the same
    setting that the Agent Card declares. Default: True.
    """
    return _env("JANUS_A2A_AUTH", "bearer").lower() != "none"


def a2a_bearer_token() -> str:
    """Bearer token clients must present on /a2a/* requests.

    Reads JANUS_A2A_TOKEN; returns "" when unset (which means
    auth is misconfigured — the auth_required() check fires but
    no token will ever match).
    """
    return _env("JANUS_A2A_TOKEN", "")


# ============================================================
# v1.40.1 — Task lifecycle + JSON-RPC dispatch (Phase 10.4.1)
# ============================================================
#
# A2A's /a2a endpoint is JSON-RPC 2.0. Methods:
#   tasks/send    — submit a task; we execute synchronously and
#                   return a COMPLETED task with artifacts
#   tasks/get     — retrieve a task's current state
#   tasks/cancel  — request cancellation
#
# Tasks persist to ~/.janus/a2a/tasks/<task_id>.json so a server
# restart doesn't lose state, and tasks/get works after the
# original tasks/send returned.

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional


# Task lifecycle states (per A2A spec naming).
STATE_SUBMITTED = "submitted"
STATE_WORKING = "working"
STATE_INPUT_REQUIRED = "input-required"
STATE_COMPLETED = "completed"
STATE_CANCELED = "canceled"
STATE_FAILED = "failed"


@dataclass
class TaskMessage:
    """One message in the task history. Mirrors A2A's Message shape:
    role + parts. We support text parts in v1.40.1; v1.40.x can
    extend to file/data parts."""
    role: str                       # "user" | "agent"
    parts: list[dict] = field(default_factory=list)


@dataclass
class TaskArtifact:
    """An A2A artifact — the agent's output(s). v1.40.1 produces
    one artifact per task with text parts containing the final
    response. Future revs can attach file artifacts."""
    name: str
    parts: list[dict] = field(default_factory=list)


@dataclass
class Task:
    id: str
    sessionId: str                  # A2A camelCase per spec
    state: str = STATE_SUBMITTED
    message: Optional[dict] = None  # latest status update
    artifacts: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _tasks_dir():
    from . import config
    d = config.HOME / "a2a" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _task_path(task_id: str):
    return _tasks_dir() / f"{task_id}.json"


def _save_task(task: Task) -> None:
    task.updated_at = time.time()
    p = _task_path(task.id)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")
    import os as _os
    _os.replace(tmp, p)


def load_task(task_id: str) -> Optional[Task]:
    p = _task_path(task_id)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    return Task(
        id=str(raw.get("id", "")),
        sessionId=str(raw.get("sessionId", "")),
        state=str(raw.get("state", STATE_SUBMITTED)),
        message=raw.get("message"),
        artifacts=raw.get("artifacts") or [],
        history=raw.get("history") or [],
        created_at=float(raw.get("created_at", time.time())),
        updated_at=float(raw.get("updated_at", time.time())),
    )


def _extract_user_text(message: Any) -> str:
    """Pull text out of an A2A message's parts."""
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    if not isinstance(parts, list):
        return ""
    out = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            out.append(str(p.get("text", "")))
        elif "text" in p:
            out.append(str(p.get("text", "")))
    return "\n".join(t for t in out if t).strip()


# ---------- JSON-RPC method handlers ----------


def _handle_tasks_send(params: dict) -> dict:
    """Submit a new task and execute it synchronously.

    Spec params:
      {
        "id": "<task-uuid>",  # client-provided OR we generate
        "sessionId": "<session-uuid>",  # optional
        "message": {"role": "user", "parts": [{"type":"text","text":"..."}]}
      }

    v1.40.1 runs the agent's chat turn synchronously and returns a
    COMPLETED task with one artifact carrying the final text.
    """
    if not isinstance(params, dict):
        raise _RpcError(-32602, "Invalid params: expected object")

    task_id = str(params.get("id") or "").strip() or uuid.uuid4().hex
    session_id = str(params.get("sessionId") or "").strip() or uuid.uuid4().hex
    message = params.get("message")
    user_text = _extract_user_text(message)
    if not user_text:
        raise _RpcError(
            -32602, "Invalid params: message.parts must contain non-empty text",
        )

    task = Task(id=task_id, sessionId=session_id, state=STATE_WORKING)
    if isinstance(message, dict):
        task.history.append(message)
    _save_task(task)

    # Run the chat turn synchronously.
    try:
        from . import app as janus_app
        from .tools import default_registry, make_protected
        from .tools.capabilities import CapabilitySet
        from . import permissions, config as _config
        from . import memory as _memory

        mode = permissions.normalize(_config.APPROVAL_MODE)
        # Auto-approve for A2A — the calling agent is trusted via
        # bearer auth at the HTTP layer. v1.40.x can refine this
        # with capability tokens declared in the agent card.
        approver = lambda *a, **kw: True  # noqa: E731
        caps = CapabilitySet()
        tools = default_registry(capabilities=caps)
        approver = make_protected(approver, caps, mode)
        preamble = ""
        try:
            preamble = _memory.prepend_for_prompt() or ""
        except Exception:
            preamble = ""

        output, trace = janus_app.run_turn(
            messages=[],
            user_input=user_text,
            tools=tools,
            approver=approver,
            memory_preamble=preamble,
            mode=mode,
            workspace=str(_config.WORKSPACE),
            tool_count=len(tools.names()),
            skill_count=0,
            stream=False,
        )
    except Exception as e:
        task.state = STATE_FAILED
        task.message = {
            "role": "agent",
            "parts": [{"type": "text", "text": f"execution failed: {type(e).__name__}: {e}"}],
        }
        _save_task(task)
        return asdict(task)

    # Wrap output as a single artifact.
    artifact = TaskArtifact(
        name="response",
        parts=[{"type": "text", "text": str(output or "")}],
    )
    task.artifacts.append(asdict(artifact))
    task.state = STATE_COMPLETED
    task.message = {
        "role": "agent",
        "parts": [{"type": "text", "text": str(output or "")}],
    }
    task.history.append(task.message)
    _save_task(task)
    return asdict(task)


def _handle_tasks_get(params: dict) -> dict:
    if not isinstance(params, dict):
        raise _RpcError(-32602, "Invalid params: expected object")
    task_id = str(params.get("id") or "").strip()
    if not task_id:
        raise _RpcError(-32602, "Invalid params: id required")
    task = load_task(task_id)
    if task is None:
        raise _RpcError(-32001, f"Task not found: {task_id}")
    return asdict(task)


def _handle_tasks_cancel(params: dict) -> dict:
    if not isinstance(params, dict):
        raise _RpcError(-32602, "Invalid params: expected object")
    task_id = str(params.get("id") or "").strip()
    if not task_id:
        raise _RpcError(-32602, "Invalid params: id required")
    task = load_task(task_id)
    if task is None:
        raise _RpcError(-32001, f"Task not found: {task_id}")
    # v1.40.1 executes synchronously — by the time tasks/cancel is
    # callable the task is already in a terminal state. Treat cancel
    # as a no-op when state is terminal; otherwise mark canceled.
    if task.state in (STATE_COMPLETED, STATE_FAILED, STATE_CANCELED):
        return asdict(task)
    task.state = STATE_CANCELED
    _save_task(task)
    return asdict(task)


# ---------- JSON-RPC dispatch ----------


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_METHODS = {
    "tasks/send": _handle_tasks_send,
    "tasks/get": _handle_tasks_get,
    "tasks/cancel": _handle_tasks_cancel,
}


def dispatch(envelope: dict) -> dict:
    """Dispatch a JSON-RPC 2.0 request envelope. Returns the
    response envelope dict.

    Envelope shape:
      {"jsonrpc": "2.0", "method": "tasks/send", "params": {...}, "id": <any>}

    Returns:
      {"jsonrpc": "2.0", "result": {...}, "id": <same>}    on success
      {"jsonrpc": "2.0", "error": {"code": ..., "message": ...}, "id": <same>}    on error
    """
    if not isinstance(envelope, dict):
        return _rpc_err(None, -32600, "Invalid Request: not a JSON object")
    if envelope.get("jsonrpc") != "2.0":
        return _rpc_err(envelope.get("id"), -32600,
                        "Invalid Request: jsonrpc must be '2.0'")
    method = envelope.get("method")
    rid = envelope.get("id")
    if not isinstance(method, str):
        return _rpc_err(rid, -32600, "Invalid Request: method required")
    handler = _METHODS.get(method)
    if handler is None:
        return _rpc_err(rid, -32601, f"Method not found: {method}")
    params = envelope.get("params") or {}
    try:
        result = handler(params)
    except _RpcError as e:
        return _rpc_err(rid, e.code, e.message)
    except Exception as e:  # last-resort safety net
        return _rpc_err(rid, -32603, f"Internal error: {type(e).__name__}: {e}")
    return {"jsonrpc": "2.0", "result": result, "id": rid}


def _rpc_err(rid, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": rid,
    }
