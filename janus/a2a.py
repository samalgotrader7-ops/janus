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
