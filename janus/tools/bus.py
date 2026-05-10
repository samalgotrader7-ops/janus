"""
tools/bus.py — model-callable tools for the inter-agent message
bus (v1.39.1, Phase 10.3.1).

These wrap the message_bus module so subagents (and the parent
agent) can coordinate via the shared bus. Two tools:

  bus_send(run_id, body, from_agent=?, kind="msg")
    Append a message to the run's log. risk='write' (low — append-
    only, no overwrites possible).

  bus_recv(run_id, since=None, from_agent=None, limit=None)
    Read messages. risk='read' — pure observation.

Both tools take run_id explicitly. Future v1.39.2+ may auto-thread
run_id through subagent context so the model doesn't need to pass
it; for v1.39.1 we keep it explicit.

Capability tokens: 'bus.send' and 'bus.recv' — skills can grant
subagent groups access without the per-call y/n. Default approver
prompts on bus_send; bus_recv is risk='read' so under default mode
it auto-allows.
"""

from __future__ import annotations

from . import base
from .. import message_bus


class BusSend(base.Tool):
    name = "bus_send"
    description = (
        "Append a message to a run's inter-agent message bus. "
        "Used to coordinate with sibling agents in the same swarm / "
        "subagent group. Append-only — past messages are never "
        "overwritten. Body must be JSON-serializable (string, dict, "
        "list of those). Pass from_agent so receivers know who "
        "spoke; defaults to None (anonymous). Kind defaults to 'msg' "
        "but you can use 'status' / 'error' to tag messages for "
        "filtering."
    )
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": (
                    "Run identifier the message attaches to. Must "
                    "match what other agents in the same group use."
                ),
            },
            "body": {
                "description": (
                    "Message payload — JSON-serializable: string, "
                    "dict, list, number, bool, null."
                ),
            },
            "from_agent": {
                "type": "string",
                "description": (
                    "Optional sender id. Lets receivers filter "
                    "messages by sender."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Message kind — 'msg' (default), 'status', "
                    "'error', or any string. Receivers can filter "
                    "by kind."
                ),
            },
        },
        "required": ["run_id", "body"],
    }
    dangerous = True
    risk = "write"

    def run(self, args: dict, approver: base.Approver) -> str:
        run_id = (args.get("run_id") or "").strip()
        if not run_id:
            return "bus_send: run_id required"
        body = args.get("body")
        if body is None:
            return "bus_send: body required"
        from_agent = args.get("from_agent")
        kind = args.get("kind") or "msg"

        # Approval (skills can grant via bus.send capability)
        body_preview = str(body)
        if len(body_preview) > 200:
            body_preview = body_preview[:197] + "..."
        details = (
            f"run_id:     {run_id}\n"
            f"from_agent: {from_agent or '(none)'}\n"
            f"kind:       {kind}\n"
            f"body:       {body_preview}"
        )
        ok = approver(
            "bus.send",
            details,
            capability=("bus", "send", run_id),
        )
        if not ok:
            return "bus_send: refused by user."

        try:
            msg = message_bus.send(
                run_id,
                body,
                from_agent=from_agent if isinstance(from_agent, str) else None,
                kind=str(kind),
            )
        except (ValueError, OSError) as e:
            return f"bus_send: {type(e).__name__}: {e}"

        return f"bus_send: ok (ts={msg.ts:.3f})"


class BusRecv(base.Tool):
    name = "bus_recv"
    description = (
        "Read messages from a run's inter-agent message bus. "
        "Returns messages ordered oldest-to-newest as JSON. "
        "Filters: since=<unix_ts> (strictly newer than ts), "
        "from_agent=<id> (only that sender), limit=<int> (cap "
        "to N most-recent matches). All filters compose."
    )
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "Run id to read from.",
            },
            "since": {
                "type": "number",
                "description": (
                    "Unix timestamp (seconds). Only messages with "
                    "ts > since are returned. Use the ts of the last "
                    "message you saw to poll incrementally."
                ),
            },
            "from_agent": {
                "type": "string",
                "description": (
                    "Filter to messages from this sender id only."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Cap result count to N most-recent matches. "
                    "Omit to get all."
                ),
            },
        },
        "required": ["run_id"],
    }
    dangerous = False  # read-only — auto-allows under default mode
    risk = "read"

    def run(self, args: dict, approver: base.Approver) -> str:
        run_id = (args.get("run_id") or "").strip()
        if not run_id:
            return "bus_recv: run_id required"
        since = args.get("since")
        from_agent = args.get("from_agent")
        limit = args.get("limit")

        # Coerce numeric/int args; ignore garbage
        try:
            since_f = float(since) if since is not None else None
        except (TypeError, ValueError):
            since_f = None
        try:
            limit_i = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            limit_i = None

        try:
            msgs = message_bus.recv(
                run_id,
                since=since_f,
                from_agent=from_agent if isinstance(from_agent, str) else None,
                limit=limit_i,
            )
        except OSError as e:
            return f"bus_recv: {type(e).__name__}: {e}"

        if not msgs:
            return "bus_recv: (no messages)"

        import json as _json
        # Render as JSON array — easiest for the model to parse.
        rendered = [
            {
                "ts": m.ts,
                "from_agent": m.from_agent,
                "kind": m.kind,
                "body": m.body,
            }
            for m in msgs
        ]
        return _json.dumps(rendered, indent=2)
