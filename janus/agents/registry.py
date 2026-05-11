"""
agents/registry.py — discovery + dispatch for first-class agents.

DISCOVERY:
  1. Bundled agents at janus/agents/bundled/<name>/agent.py — each
     such file must expose a module-level `AGENT: Agent` variable.
  2. User-defined agents at ~/.janus/agents/<name>/manifest.json —
     identity + skills declared as JSON. Tools are looked up in the
     global janus.tools registry by name.

User-defined wins on name conflict. This is intentional so users can
override a bundled agent without editing site-packages.

API:
  list_agents()  → list[Agent]
  load_agent(name) → Agent | None
  dispatch(name, prompt, approver?, cwd?) → str
  agents_dir() → Path to ~/.janus/agents/ (created on first read)

USED BY:
  * janus/mcp/server.py — exposes janus_agent_list +
    janus_agent_dispatch tools to Claude Code over MCP.
  * Surfaces (cli_rich, telegram, web) — future /agent slash command
    will route through dispatch(). Not wired in v1.41.0; the MCP
    server is the priority surface for this release.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from pathlib import Path
from typing import Any, Callable

from .. import config
from .base import Agent
from .identity import AgentIdentity
from .memory import AgentMemory
from .skills import AgentSkill


log = logging.getLogger("janus.agents")


def agents_dir() -> Path:
    """User-defined agents root: ~/.janus/agents/ — created on demand."""
    d = config.HOME / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iter_bundled_agents() -> list[Agent]:
    """Walk janus/agents/bundled/* for packages that expose an AGENT.

    A bundled agent is a package (subdir with __init__.py) containing
    an `agent` submodule whose top-level `AGENT` attribute is an
    Agent instance. Anything malformed is logged and skipped — a
    broken bundled agent should not break the registry.
    """
    out: list[Agent] = []
    try:
        from . import bundled  # noqa: F401  — package import
    except ModuleNotFoundError:
        return out
    bundled_path = Path(__file__).parent / "bundled"
    if not bundled_path.is_dir():
        return out
    for entry in sorted(bundled_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        module_path = f"janus.agents.bundled.{entry.name}.agent"
        try:
            mod = importlib.import_module(module_path)
        except Exception as e:
            log.warning("bundled agent %s import failed: %s: %s",
                        entry.name, type(e).__name__, e)
            continue
        agent = getattr(mod, "AGENT", None)
        if not isinstance(agent, Agent):
            log.warning("bundled agent %s: no Agent in AGENT attribute",
                        entry.name)
            continue
        out.append(agent)
    return out


def _iter_user_agents() -> list[Agent]:
    """Walk ~/.janus/agents/* for manifest.json-defined agents."""
    out: list[Agent] = []
    root = agents_dir()
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("user agent %s: manifest parse failed: %s: %s",
                        entry.name, type(e).__name__, e)
            continue
        if not isinstance(raw, dict):
            log.warning("user agent %s: manifest is not a JSON object",
                        entry.name)
            continue
        try:
            identity = AgentIdentity.from_dict(raw)
        except ValueError as e:
            log.warning("user agent %s: identity invalid: %s",
                        entry.name, e)
            continue
        skills_raw = raw.get("skills") or []
        skills: list[AgentSkill] = []
        if isinstance(skills_raw, list):
            for s in skills_raw:
                if not isinstance(s, dict):
                    continue
                try:
                    skills.append(AgentSkill.from_dict(s))
                except ValueError as e:
                    log.warning("user agent %s: skill skipped: %s",
                                entry.name, e)
        out.append(Agent(
            identity=identity,
            memory=AgentMemory(identity.name),
            skills=skills,
        ))
    return out


def list_agents() -> list[Agent]:
    """Discover all available agents. User overrides bundled on name conflict."""
    bundled = _iter_bundled_agents()
    user = _iter_user_agents()
    by_name: dict[str, Agent] = {a.name: a for a in bundled}
    for a in user:
        by_name[a.name] = a  # user wins
    return [by_name[k] for k in sorted(by_name.keys())]


def load_agent(name: str) -> Agent | None:
    """Return the named agent, or None if not found."""
    name = (name or "").strip()
    if not name:
        return None
    for a in list_agents():
        if a.name == name:
            return a
    return None


def dispatch(
    name: str,
    prompt: str,
    approver: Callable[..., bool] | None = None,
    cwd: str | None = None,
    extra_args: dict[str, Any] | None = None,
) -> str:
    """Look up the agent by name and run it. Returns the output text.

    If the agent doesn't exist, returns a clear error string (so
    callers like the MCP server can surface it to Claude Code without
    raising).
    """
    agent = load_agent(name)
    if agent is None:
        known = ", ".join(a.name for a in list_agents()) or "(none)"
        return f"agent '{name}' not found. Known agents: {known}"
    return agent.run(prompt, approver=approver, cwd=cwd, extra_args=extra_args)
