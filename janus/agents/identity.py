"""
agents/identity.py — AgentIdentity dataclass.

An identity is who the agent is: name, what it does, what tools it
uses, what model it prefers, what system prompt frames its behavior.
Static once loaded — agents don't mutate their own identity (skills
and memory cover dynamic state).

WIRE FORMAT:
User-defined agents declare identity in `~/.janus/agents/<name>/manifest.json`
with the following shape:

  {
    "name": "researcher",
    "description": "Web + memory search specialist",
    "system_prompt": "You are a research specialist...",
    "model": "anthropic/claude-sonnet-4-6",
    "tool_names": ["web_fetch", "web_search", "memory_search"],
    "tags": ["research"],
    "style": "chat",
    "version": "1.0"
  }

`model` is optional; when unset the agent runs with the global
config.MODEL (the user's default). Useful when you want a specific
agent to always use a stronger / cheaper / domain-specific model.

`style` declares how the MCP dispatch handler should run this agent:
  * "chat"     - default. Full LLM turn through janus.app with the
                 declared tools + system prompt.
  * "wrapper"  - thin pass-through to a single tool. Skip the LLM
                 turn; just call tool_names[0].run({"prompt": ...}).
                 Used by the bundled `claude` agent which delegates
                 every call to the `claude_code` tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentIdentity:
    name: str
    description: str = ""
    system_prompt: str = ""
    model: str | None = None
    tool_names: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    style: str = "chat"  # "chat" | "wrapper"
    version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "model": self.model,
            "tool_names": list(self.tool_names),
            "tags": list(self.tags),
            "style": self.style,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentIdentity":
        name = str(d.get("name") or "").strip()
        if not name:
            raise ValueError("AgentIdentity.from_dict: 'name' is required")
        style = str(d.get("style") or "chat").strip().lower()
        if style not in ("chat", "wrapper"):
            style = "chat"
        return cls(
            name=name,
            description=str(d.get("description") or ""),
            system_prompt=str(d.get("system_prompt") or ""),
            model=(str(d["model"]) if d.get("model") else None),
            tool_names=[str(t) for t in (d.get("tool_names") or [])],
            tags=[str(t) for t in (d.get("tags") or [])],
            style=style,
            version=str(d.get("version") or "1.0"),
        )
