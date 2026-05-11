"""
agents/skills.py — AgentSkill: one named workflow attached to an agent.

WHY:
An identity says WHO an agent is. A skill says WHAT a known workflow
looks like. An agent typically has 1-5 skills; the dispatcher matches
the user's prompt to the closest skill (or runs general-purpose if
none matches).

This is intentionally different from janus.skills (the global skill
library at ~/.janus/skills/). Agent-skills are scoped to one agent,
travel with the agent's manifest, and don't pollute the global
skill catalog.

DESIGN:
  * Pure data — name, description, when_to_use, body.
  * `body` is appended to the agent's system prompt when this skill
    is the matched one for the current turn.
  * No execution logic here; the agent's run() decides what skill
    (if any) to apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentSkill:
    name: str
    description: str = ""
    when_to_use: str = ""
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "body": self.body,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentSkill":
        name = str(d.get("name") or "").strip()
        if not name:
            raise ValueError("AgentSkill.from_dict: 'name' is required")
        return cls(
            name=name,
            description=str(d.get("description") or ""),
            when_to_use=str(d.get("when_to_use") or ""),
            body=str(d.get("body") or ""),
        )
