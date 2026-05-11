"""
Bundled 'claude' agent — Janus's primary peer in the bidirectional
Claude Code ↔ Janus bridge (v1.41.0, Phase 11.0).

WHY:
Sam wants to talk to Janus on any surface (Telegram, web, CLI) and
have Janus delegate coding work to Claude Code transparently. The
inverse direction (Claude Code calling into Janus) is solved by the
MCP server. This agent solves the forward direction:

  Sam → Janus surface → @claude → THIS AGENT → ClaudeCode tool → claude -p

Because identity.style == "wrapper", Agent.run() bypasses the
LLM turn entirely. The prompt is passed straight to the
`claude_code` tool — single subprocess, one round-trip.

USAGE:
  - From a Janus chat / Telegram / web turn, the dispatcher routes
    "@claude <prompt>" to this agent (wiring landing in v1.41.x).
  - From the MCP server: janus_agent_dispatch(name="claude",
    prompt="...") in Claude Code itself. (Loop-detection lives at
    the ClaudeCode tool layer — that subprocess sets the
    no-recursion env var Claude Code respects.)

MODEL:
  identity.model is unset. The wrapper does not influence which
  model Claude Code uses; that's controlled by the user's
  `claude config` or env. If Sam wires his Max-plan auth, this
  inherits it automatically.
"""

from __future__ import annotations

from ...base import Agent
from ...identity import AgentIdentity
from ...skills import AgentSkill


_IDENTITY = AgentIdentity(
    name="claude",
    description=(
        "Hand off coding work to the external Claude Code CLI "
        "(claude -p). Use when the user explicitly asks for Claude, "
        "or when Janus decides Claude is a better fit (large refactor, "
        "needs Anthropic-specific reasoning style, etc.)."
    ),
    system_prompt="",  # wrapper style — system prompt unused
    model=None,
    tool_names=["claude_code"],
    tags=["external-cli", "delegation", "code"],
    style="wrapper",
    version="1.0",
)


_SKILLS = [
    AgentSkill(
        name="delegate-coding",
        description="Hand off a focused coding sub-task to Claude Code.",
        when_to_use=(
            "User mentions Claude / Anthropic by name, or the task is "
            "a non-trivial multi-file code change that benefits from "
            "Claude Code's planning + edit tools."
        ),
        body="",  # body unused for wrappers — kept as documentation only
    ),
]


AGENT = Agent(identity=_IDENTITY, skills=_SKILLS)
