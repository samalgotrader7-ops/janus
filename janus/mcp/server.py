"""
mcp/server.py — Janus MCP server (v1.41.0, Phase 11.0).

WHY:
With v1.40.x Janus speaks A2A over HTTP. That's the right protocol for
agent-to-agent across the network, but it's heavy when the OTHER side
is Claude Code running on the same machine — every call pays an HTTP
round-trip plus FastAPI boot. The Model Context Protocol (MCP) is the
standard Claude Code uses to absorb in-process tools over stdio. By
shipping an MCP server, Janus becomes a first-class set of `janus_*`
tools inside Claude Code with no HTTP and no auth gymnastics.

WIRE FORMAT (per Anthropic's MCP spec):
  * JSON-RPC 2.0, line-delimited JSON over stdio.
  * stdout: protocol traffic only. NEVER print()s outside the
    protocol — logging goes to stderr.
  * Methods implemented: initialize, tools/list, tools/call.
  * Notifications: notifications/initialized (acked silently).

TOOL SURFACE (v1.41.0):
  janus_agent_list                — discover available agents
  janus_agent_dispatch            — run one turn against an agent
  janus_agent_memory_get/set/note — per-agent memory
  janus_blackboard_get/set/all    — shared run-scoped kv state
  janus_bus_send/recv             — append-only inter-agent messages
  janus_a2a_card                  — read Janus's local Agent Card
  janus_a2a_dispatch              — run a tasks/send synchronously
                                    in-process (no HTTP hop)

REGISTRATION:
Add to ~/.claude/settings.json or ~/.janus/mcp/servers.json:

  {
    "mcpServers": {
      "janus": {
        "command": "python",
        "args": ["-m", "janus.mcp.server"]
      }
    }
  }

Then Claude Code (or Janus's own MCP client) spawns it on first call.

NOT IN SCOPE (v1.41.0):
  * Resources / prompts / sampling — only `tools/*` is implemented.
  * Streaming notifications during long-running tool calls.
  * HTTP/SSE transport — stdio only (mirrors Janus's own client).
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Any, Callable

# stderr-only logger. Anything we write to stdout must be protocol
# JSON; the Claude Code client will choke on stray text.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="janus-mcp %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("janus.mcp.server")


# ---------- MCP protocol constants ----------

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "janus"


def _server_version() -> str:
    try:
        from .. import branding
        return str(getattr(branding, "VERSION", "0.0.0"))
    except Exception:
        return "0.0.0"


# ---------- Tool registry ----------
#
# Each entry: name -> (description, input_schema, handler)
# Handler signature: handler(args: dict) -> str
# Errors raise; the dispatcher wraps them as { isError: true, content }.

ToolHandler = Callable[[dict], str]
_TOOLS: dict[str, tuple[str, dict, ToolHandler]] = {}


def _register(name: str, description: str, input_schema: dict, handler: ToolHandler) -> None:
    _TOOLS[name] = (description, input_schema, handler)


# ---------- Tool implementations ----------


def _tool_agent_list(_args: dict) -> str:
    from ..agents import list_agents
    agents = list_agents()
    if not agents:
        return "No agents discovered. Bundled live in janus/agents/bundled/; user-defined in ~/.janus/agents/."
    lines = []
    for a in agents:
        d = a.to_dict()
        tools = ", ".join(d["tool_names"]) or "(none)"
        lines.append(
            f"- {d['name']}  [{d['style']}]  tools=({tools})\n"
            f"    {d['description']}"
        )
    return "\n".join(lines)


def _tool_agent_dispatch(args: dict) -> str:
    from ..agents import dispatch
    name = str(args.get("name") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    cwd = str(args.get("cwd") or "").strip() or None
    if not name:
        return "error: 'name' is required"
    if not prompt:
        return "error: 'prompt' is required"
    extra = args.get("extra_args")
    if extra is not None and not isinstance(extra, dict):
        return "error: 'extra_args' must be an object"
    return dispatch(name, prompt, cwd=cwd, extra_args=extra)


def _tool_agent_memory_get(args: dict) -> str:
    from ..agents import AgentMemory
    name = str(args.get("agent") or "").strip()
    key = str(args.get("key") or "").strip()
    if not name or not key:
        return "error: 'agent' and 'key' are required"
    val = AgentMemory(name).get(key)
    if val is None:
        return f"(no value set for '{key}' on agent '{name}')"
    if isinstance(val, str):
        return val
    return json.dumps(val, indent=2)


def _tool_agent_memory_set(args: dict) -> str:
    from ..agents import AgentMemory
    name = str(args.get("agent") or "").strip()
    key = str(args.get("key") or "").strip()
    if not name or not key:
        return "error: 'agent' and 'key' are required"
    if "value" not in args:
        return "error: 'value' is required"
    AgentMemory(name).set(key, args["value"])
    return f"ok: set {name}.{key}"


def _tool_agent_memory_note(args: dict) -> str:
    from ..agents import AgentMemory
    name = str(args.get("agent") or "").strip()
    text = str(args.get("text") or "").strip()
    if not name or not text:
        return "error: 'agent' and 'text' are required"
    AgentMemory(name).append_note(text)
    return f"ok: note appended to {name} (1 entry)"


def _tool_blackboard_get(args: dict) -> str:
    from .. import blackboard
    run_id = str(args.get("run_id") or "").strip()
    key = str(args.get("key") or "").strip()
    if not run_id or not key:
        return "error: 'run_id' and 'key' are required"
    val = blackboard.get(run_id, key)
    if val is None:
        return f"(no value for '{key}' in run '{run_id}')"
    if isinstance(val, str):
        return val
    return json.dumps(val, indent=2)


def _tool_blackboard_set(args: dict) -> str:
    from .. import blackboard
    run_id = str(args.get("run_id") or "").strip()
    key = str(args.get("key") or "").strip()
    if not run_id or not key:
        return "error: 'run_id' and 'key' are required"
    if "value" not in args:
        return "error: 'value' is required"
    blackboard.set_value(run_id, key, args["value"])
    return f"ok: blackboard[{run_id}][{key}] set"


def _tool_blackboard_all(args: dict) -> str:
    from .. import blackboard
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return "error: 'run_id' is required"
    return json.dumps(blackboard.all_for(run_id), indent=2)


def _tool_bus_send(args: dict) -> str:
    from .. import message_bus
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return "error: 'run_id' is required"
    if "body" not in args:
        return "error: 'body' is required"
    from_agent = args.get("from_agent")
    kind = str(args.get("kind") or "msg")
    msg = message_bus.send(
        run_id,
        args["body"],
        from_agent=str(from_agent) if from_agent else None,
        kind=kind,
    )
    return f"ok: sent ts={msg.ts:.6f}"


def _tool_bus_recv(args: dict) -> str:
    from .. import message_bus
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return "error: 'run_id' is required"
    since_raw = args.get("since")
    since: float | None
    try:
        since = float(since_raw) if since_raw is not None else None
    except (TypeError, ValueError):
        since = None
    limit_raw = args.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else None
    except (TypeError, ValueError):
        limit = None
    msgs = message_bus.recv(run_id, since=since, limit=limit)
    return json.dumps(
        [{"ts": m.ts, "from_agent": m.from_agent,
          "kind": m.kind, "body": m.body} for m in msgs],
        indent=2,
    )


def _tool_a2a_card(_args: dict) -> str:
    from .. import a2a
    return json.dumps(a2a.build_agent_card(), indent=2)


def _tool_skill_gepa(args: dict) -> str:
    """Run a GEPA pass on a skill and return a text summary.

    By default the artifact is persisted under
    ``~/.janus/skills/_gepa/<skill>/<run_id>.json`` AND, when
    recommendation == "apply" and ``apply`` is true, the new body is
    written to disk. The default ``apply=false`` keeps P4: the caller
    reads the summary, optionally fetches the artifact, then decides.
    """
    from .. import skill_gepa
    name = str(args.get("skill") or "").strip()
    if not name:
        return "error: 'skill' is required"

    def _int_opt(key: str) -> int | None:
        raw = args.get(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    generations = _int_opt("generations")
    population = _int_opt("population")
    record_count = _int_opt("record_count")
    max_calls = _int_opt("max_llm_calls")
    seed = _int_opt("seed")

    try:
        result = skill_gepa.evolve(
            name,
            generations=generations,
            population=population,
            record_count=record_count,
            max_llm_calls=max_calls,
            seed=seed,
        )
    except Exception as e:
        return f"error: GEPA failed: {type(e).__name__}: {e}"

    summary = skill_gepa.render_result(result, include_diff=False)

    apply_flag = bool(args.get("apply"))
    if apply_flag and result.recommendation == "apply":
        try:
            skill_gepa.apply_best(result)
            summary += "\n\n[applied] new body persisted to skill file."
        except ValueError as e:
            summary += f"\n\n[apply-skipped] {e}"
    elif apply_flag:
        summary += (
            f"\n\n[apply-skipped] recommendation={result.recommendation} "
            "(set apply=false or wait for an 'apply' recommendation)"
        )

    return summary


def _tool_a2a_dispatch(args: dict) -> str:
    """Run a tasks/send request in-process (no HTTP) and return text.

    Useful when Claude Code wants to run a full Janus chat turn but
    doesn't want to manage a `janus web` HTTP server. Bypasses bearer
    auth — the MCP boundary is the trust boundary.
    """
    from .. import a2a
    import uuid
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "error: 'prompt' is required"
    session_id = str(args.get("session_id") or "").strip() or uuid.uuid4().hex
    envelope = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "id": uuid.uuid4().hex,
            "sessionId": session_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": prompt}],
            },
        },
        "id": 1,
    }
    resp = a2a.dispatch(envelope)
    if "error" in resp:
        err = resp["error"] if isinstance(resp.get("error"), dict) else {}
        return f"a2a error {err.get('code', '?')}: {err.get('message', '?')}"
    result = resp.get("result") or {}
    state = result.get("state", "?")
    # Pull artifact text out of the Task shape.
    artifacts = result.get("artifacts") or []
    for art in reversed(artifacts):
        if not isinstance(art, dict):
            continue
        for p in art.get("parts") or []:
            if isinstance(p, dict) and p.get("text"):
                return str(p["text"])
    msg = result.get("message") or {}
    for p in (msg.get("parts") or []):
        if isinstance(p, dict) and p.get("text"):
            return str(p["text"])
    return f"(a2a task state={state}, no text artifact)"


# ---------- Schemas + registration ----------


_register(
    "janus_agent_list",
    (
        "List all discoverable Janus agents (bundled + user-defined). "
        "Returns a text summary including name, style, declared tools, "
        "and description for each."
    ),
    {"type": "object", "properties": {}},
    _tool_agent_list,
)

_register(
    "janus_agent_dispatch",
    (
        "Run one turn against a Janus agent and return its output text. "
        "Use this to delegate a sub-task to a specialized Janus agent — "
        "the agent's tools and skills are scoped per its identity. "
        "Wrapper-style agents pass the prompt straight to a single tool; "
        "chat-style agents run a full LLM turn against the agent's "
        "declared toolset."
    ),
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Agent name (use janus_agent_list to discover).",
            },
            "prompt": {
                "type": "string",
                "description": "The instruction to send. The agent has no other context.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional working directory for tools the agent runs.",
            },
            "extra_args": {
                "type": "object",
                "description": "Optional extra args merged into the tool call (wrapper agents).",
            },
        },
        "required": ["name", "prompt"],
    },
    _tool_agent_dispatch,
)

_register(
    "janus_agent_memory_get",
    "Read a single key from an agent's persistent memory (kv.json).",
    {
        "type": "object",
        "properties": {
            "agent": {"type": "string"},
            "key": {"type": "string"},
        },
        "required": ["agent", "key"],
    },
    _tool_agent_memory_get,
)

_register(
    "janus_agent_memory_set",
    "Write a key/value to an agent's persistent memory. Value must be JSON-serializable.",
    {
        "type": "object",
        "properties": {
            "agent": {"type": "string"},
            "key": {"type": "string"},
            "value": {},
        },
        "required": ["agent", "key", "value"],
    },
    _tool_agent_memory_set,
)

_register(
    "janus_agent_memory_note",
    "Append a timestamped note to an agent's notes.md.",
    {
        "type": "object",
        "properties": {
            "agent": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["agent", "text"],
    },
    _tool_agent_memory_note,
)

_register(
    "janus_blackboard_get",
    "Read a value from the Janus blackboard (shared run-scoped kv state).",
    {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "key": {"type": "string"},
        },
        "required": ["run_id", "key"],
    },
    _tool_blackboard_get,
)

_register(
    "janus_blackboard_set",
    "Write a value to the Janus blackboard. Value must be JSON-serializable.",
    {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "key": {"type": "string"},
            "value": {},
        },
        "required": ["run_id", "key", "value"],
    },
    _tool_blackboard_set,
)

_register(
    "janus_blackboard_all",
    "Return all key/value pairs for a run as JSON.",
    {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    _tool_blackboard_all,
)

_register(
    "janus_bus_send",
    "Append a message to the Janus inter-agent message bus for a run.",
    {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "body": {},
            "from_agent": {"type": "string"},
            "kind": {"type": "string", "description": "msg | status | error"},
        },
        "required": ["run_id", "body"],
    },
    _tool_bus_send,
)

_register(
    "janus_bus_recv",
    "Read messages from the bus for a run. Optional since (ts) and limit.",
    {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "since": {"type": "number"},
            "limit": {"type": "integer"},
        },
        "required": ["run_id"],
    },
    _tool_bus_recv,
)

_register(
    "janus_skill_gepa",
    (
        "Run GEPA — the offline evolutionary skill engine — on a named "
        "Janus skill. Evolves the skill body across N generations of M "
        "variants each, scored by LLM-judged replay over the skill's "
        "historical log records, and returns a text summary including "
        "baseline vs best fitness, recommendation, and the JSON artifact "
        "path with full provenance. P4 invariant: by default does NOT "
        "persist the new body — pass apply=true to write the recommended "
        "body, but only the caller (humans / orchestrators) is the gate."
    ),
    {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name (case-sensitive, kebab-case).",
            },
            "generations": {
                "type": "integer",
                "description": "Generations to evolve (default JANUS_GEPA_GENERATIONS, normally 3).",
            },
            "population": {
                "type": "integer",
                "description": "Variants per generation (default JANUS_GEPA_POPULATION, normally 6).",
            },
            "record_count": {
                "type": "integer",
                "description": "Replay records to score against (default 10).",
            },
            "max_llm_calls": {
                "type": "integer",
                "description": "Hard cap on LLM calls per run (default 250).",
            },
            "seed": {
                "type": "integer",
                "description": "Optional seed for the variant-selection RNG (best-effort determinism).",
            },
            "apply": {
                "type": "boolean",
                "description": "If true AND recommendation==apply, persist the new body atomically.",
            },
        },
        "required": ["skill"],
    },
    _tool_skill_gepa,
)

_register(
    "janus_a2a_card",
    "Return Janus's local A2A Agent Card as JSON (what /.well-known/agent.json serves).",
    {"type": "object", "properties": {}},
    _tool_a2a_card,
)

_register(
    "janus_a2a_dispatch",
    (
        "Run a tasks/send request in-process against Janus and return the "
        "agent's final text. Bypasses HTTP — useful when Claude Code wants "
        "to drive a full Janus chat turn without a running janus web server."
    ),
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "session_id": {"type": "string"},
        },
        "required": ["prompt"],
    },
    _tool_a2a_dispatch,
)


# ---------- JSON-RPC dispatch ----------


def _handle_initialize(_params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": _server_version()},
    }


def _handle_tools_list(_params: dict) -> dict:
    tools = []
    for name, (desc, schema, _h) in _TOOLS.items():
        tools.append({
            "name": name,
            "description": desc,
            "inputSchema": schema,
        })
    return {"tools": tools}


def _handle_tools_call(params: dict) -> dict:
    name = str(params.get("name") or "")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return {
            "content": [{"type": "text", "text": "error: 'arguments' must be an object"}],
            "isError": True,
        }
    entry = _TOOLS.get(name)
    if entry is None:
        return {
            "content": [{"type": "text", "text": f"error: unknown tool '{name}'"}],
            "isError": True,
        }
    _desc, _schema, handler = entry
    try:
        text = handler(args)
        is_error = isinstance(text, str) and text.startswith("error:")
    except Exception as e:
        log.error("tool '%s' raised: %s\n%s", name, e, traceback.format_exc())
        text = f"error: {type(e).__name__}: {e}"
        is_error = True
    return {
        "content": [{"type": "text", "text": str(text)}],
        "isError": is_error,
    }


_METHODS: dict[str, Callable[[dict], dict]] = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
}


def _rpc_response(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "result": result, "id": rid}


def _rpc_error(rid: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": rid,
    }


def dispatch_message(envelope: dict) -> dict | None:
    """Dispatch one JSON-RPC envelope. Returns response dict, or None
    for notifications (which don't expect a reply per JSON-RPC 2.0)."""
    if not isinstance(envelope, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    rid = envelope.get("id")
    is_notification = "id" not in envelope
    method = envelope.get("method")
    if not isinstance(method, str):
        if is_notification:
            return None
        return _rpc_error(rid, -32600, "method required")

    # Notifications: ack silently for known ones, ignore unknown.
    if is_notification:
        if method == "notifications/initialized":
            log.info("client signalled initialized")
        else:
            log.info("unhandled notification: %s", method)
        return None

    handler = _METHODS.get(method)
    if handler is None:
        return _rpc_error(rid, -32601, f"Method not found: {method}")
    params = envelope.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(rid, -32602, "params must be an object")
    try:
        result = handler(params)
    except Exception as e:
        log.error("method '%s' raised: %s\n%s", method, e, traceback.format_exc())
        return _rpc_error(rid, -32603, f"Internal error: {type(e).__name__}: {e}")
    return _rpc_response(rid, result)


# ---------- Main loop ----------


def main() -> int:
    """Run the MCP server reading line-delimited JSON-RPC from stdin
    and writing responses to stdout. Returns process exit code."""
    log.info("janus-mcp server starting (version %s, %d tools)",
             _server_version(), len(_TOOLS))
    stdin = sys.stdin
    stdout = sys.stdout
    while True:
        line = stdin.readline()
        if not line:
            log.info("stdin closed; exiting cleanly")
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("malformed JSON-RPC line: %s", e)
            err = _rpc_error(None, -32700, f"Parse error: {e}")
            stdout.write(json.dumps(err) + "\n")
            stdout.flush()
            continue
        response = dispatch_message(envelope)
        if response is None:
            continue
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
