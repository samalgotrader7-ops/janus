"""
janus.agents — first-class agent abstraction (v1.41.0, Phase 11.0).

WHY:
Existing `janus.tools.agent` provides scheduled-agent lifecycle (one
skill + one trigger fired by cron). This module adds the richer
abstraction Sam asked for on 2026-05-11: an Agent has an Identity
(name, role, system prompt, model preference), per-agent Memory
(persistent state separate from global memory cards), a Toolset
(subset of the global tool registry), and Skills (named workflows
the agent specialises in).

DESIGN:
  * Bundled agents live in `janus/agents/bundled/<name>/` as Python
    packages — `agent.py` exports a module-level `AGENT: Agent`.
  * User-defined agents live in `~/.janus/agents/<name>/` with a
    `manifest.json` declaring identity + tool names + skills.
  * Per-agent memory at `~/.janus/agents/<name>/memory/` (kv.json +
    notes.md). Created on first write.
  * Discovery via `registry.list_agents()`. Bundled and user-defined
    are merged; user-defined wins on name conflict so users can
    override a bundled agent without editing site-packages.

THE MCP SERVER (janus/mcp/server.py) exposes:
  janus_agent_list()                      - list discoverable agents
  janus_agent_dispatch(name, prompt)      - run one turn against the
                                            named agent and return its
                                            output text.

PUBLIC API:
  AgentIdentity - dataclass describing who the agent is
  AgentMemory   - per-agent kv + notes store
  AgentSkill    - one named workflow attached to an agent
  Agent         - composition of the above with .run(prompt)
  list_agents() - return list[Agent] (bundled ∪ user)
  load_agent(name) - hydrate by name; None if missing
  dispatch(name, prompt, approver?) - run one turn, return str
"""

from __future__ import annotations

from .identity import AgentIdentity
from .memory import AgentMemory
from .skills import AgentSkill
from .base import Agent
from .registry import list_agents, load_agent, dispatch, agents_dir

__all__ = [
    "AgentIdentity",
    "AgentMemory",
    "AgentSkill",
    "Agent",
    "list_agents",
    "load_agent",
    "dispatch",
    "agents_dir",
]
