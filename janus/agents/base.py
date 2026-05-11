"""
agents/base.py — Agent class composing Identity / Memory / Tools / Skills.

WHY:
The Agent class is the central abstraction Sam asked for: every agent
that lives inside Janus has these four things, accessible the same way
regardless of whether it was bundled or user-defined.

PUBLIC API:
  Agent.identity              - AgentIdentity (read-mostly config)
  Agent.memory                - AgentMemory (per-agent persistent state)
  Agent.skills                - list[AgentSkill] (workflows)
  Agent.tool_names            - tool ids this agent declared
  Agent.tools(registry)       - returns the Tools that match tool_names
  Agent.run(prompt, ...)      - execute one turn, return output text

RUN STYLES:
  "wrapper" — pass-through to single tool. tool_names must contain
              exactly one tool. The prompt is passed as the tool's
              "prompt" arg (works for claude_code / aider / codex_cli
              / gemini_cli — all four external CLI wrappers share that
              parameter name). Returns the tool's output.

  "chat"    — full LLM turn via janus.app.run_turn() with the agent's
              system_prompt and the subset of tools it declared.

The MCP server's `janus_agent_dispatch` tool calls Agent.run() and
returns the text to Claude Code.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .identity import AgentIdentity
from .memory import AgentMemory
from .skills import AgentSkill


# Approver signature mirrors janus.tools.base.Approver — see that file
# for the kwargs contract. We accept a permissive default that auto-
# approves everything (used when the MCP server is the caller and the
# tool risks are already gated at the MCP layer).
ApproverFn = Callable[..., bool]


def _auto_approve(*_args: Any, **_kwargs: Any) -> bool:
    return True


class Agent:
    def __init__(
        self,
        identity: AgentIdentity,
        memory: AgentMemory | None = None,
        skills: list[AgentSkill] | None = None,
    ) -> None:
        self.identity = identity
        self.memory = memory or AgentMemory(identity.name)
        self.skills = list(skills or [])

    # ---------- introspection ----------

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def tool_names(self) -> list[str]:
        return list(self.identity.tool_names)

    def to_dict(self) -> dict[str, Any]:
        """Serializable summary — used by janus_agent_list."""
        return {
            "name": self.identity.name,
            "description": self.identity.description,
            "model": self.identity.model,
            "tool_names": list(self.identity.tool_names),
            "tags": list(self.identity.tags),
            "style": self.identity.style,
            "version": self.identity.version,
            "skills": [s.to_dict() for s in self.skills],
            "memory_dir": str(self.memory.dir),
        }

    # ---------- execution ----------

    def run(
        self,
        prompt: str,
        approver: Optional[ApproverFn] = None,
        cwd: Optional[str] = None,
        extra_args: Optional[dict[str, Any]] = None,
    ) -> str:
        """Execute one turn against this agent. Returns output text.

        For style='wrapper': calls the single declared tool with
        {"prompt": prompt, **extra_args}. Output is the tool's result.

        For style='chat': constructs a tool Registry filtered to
        identity.tool_names and runs a full LLM turn via
        janus.app.run_turn() with the agent's system_prompt as a
        prepended memory preamble.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return f"{self.name}: empty prompt"

        approver = approver or _auto_approve
        extra_args = dict(extra_args or {})

        if self.identity.style == "wrapper":
            return self._run_wrapper(prompt, approver, cwd, extra_args)
        return self._run_chat(prompt, approver, cwd, extra_args)

    def _run_wrapper(
        self,
        prompt: str,
        approver: ApproverFn,
        cwd: Optional[str],
        extra_args: dict[str, Any],
    ) -> str:
        if len(self.identity.tool_names) != 1:
            return (
                f"{self.name}: 'wrapper' style requires exactly one tool, "
                f"got {self.identity.tool_names}"
            )
        tool_name = self.identity.tool_names[0]
        from ..tools import default_registry  # lazy: tools pull in heavy deps
        from ..tools.capabilities import CapabilitySet

        registry = default_registry(capabilities=CapabilitySet())
        if tool_name not in registry.names():
            return (
                f"{self.name}: declared tool '{tool_name}' is not "
                "registered. Check that the tool's import is wired in "
                "janus/tools/__init__.py."
            )
        args: dict[str, Any] = {"prompt": prompt, **extra_args}
        if cwd:
            args.setdefault("cwd", cwd)
        return registry.call(tool_name, args, approver)

    def _run_chat(
        self,
        prompt: str,
        approver: ApproverFn,
        cwd: Optional[str],
        extra_args: dict[str, Any],
    ) -> str:
        from .. import app as janus_app
        from .. import config as _config
        from .. import permissions
        from ..tools import default_registry, make_protected
        from ..tools.capabilities import CapabilitySet

        mode = permissions.normalize(_config.APPROVAL_MODE)
        caps = CapabilitySet()
        full_registry = default_registry(capabilities=caps)
        # Filter to declared tools when the agent named some; otherwise
        # the agent gets the full toolbox.
        if self.identity.tool_names:
            keep = set(self.identity.tool_names)
            for n in list(full_registry.names()):
                if n not in keep:
                    full_registry._tools.pop(n, None)  # noqa: SLF001

        protected = make_protected(approver, caps, mode)

        # Merge identity.system_prompt + matched skill body as preamble.
        preamble_parts: list[str] = []
        if self.identity.system_prompt:
            preamble_parts.append(self.identity.system_prompt.strip())
        for skill in self.skills:
            if skill.body.strip():
                preamble_parts.append(skill.body.strip())
        preamble = "\n\n".join(preamble_parts).strip()

        workspace = cwd or str(_config.WORKSPACE)
        try:
            output, _trace = janus_app.run_turn(
                messages=[],
                user_input=prompt,
                tools=full_registry,
                approver=protected,
                memory_preamble=preamble,
                mode=mode,
                workspace=workspace,
                tool_count=len(full_registry.names()),
                skill_count=len(self.skills),
                stream=False,
            )
            return str(output or "")
        except Exception as e:
            return f"{self.name}: chat-run failed: {type(e).__name__}: {e}"
