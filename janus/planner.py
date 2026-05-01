"""
planner.py — Phase 4: decompose a chosen interpretation into a plan tree.

WHAT IT DOES:
After interpretation is chosen, the planner asks the LLM to break the work
into a small tree of leaf tasks. Each leaf is a discrete sub-goal the
executor can complete in PLAN_LEAF_STEPS or fewer tool calls.

WHY:
Linear executor + 25-step cap == "stopped at step limit" on multi-tool
projects (X agent build, two-stage refactors). Trees let each sub-goal
get its own bounded budget AND its own skill+capability scope.

WHAT THE TREE LOOKS LIKE:
{
  "goal": "Build the X→Telegram news agent",
  "children": [
    {"id": "a", "goal": "Scaffold Twitter client",      "skill": null, "deps": []},
    {"id": "b", "goal": "Add news aggregation module",  "skill": null, "deps": ["a"]},
    {"id": "c", "goal": "Wire Telegram delivery",       "skill": null, "deps": ["b"]},
    {"id": "d", "goal": "Write README + Dockerfile",    "skill": null, "deps": ["c"]}
  ]
}

For simple requests the planner returns a one-leaf tree (the orchestrator
collapses that to the original linear executor — same behavior as Phases 1-3).

The planner does NOT execute. The orchestrator does.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from . import config, llm


@dataclass
class PlanNode:
    id: str
    goal: str
    skill: str | None = None
    deps: list[str] = field(default_factory=list)
    children: list["PlanNode"] = field(default_factory=list)
    # Phase 8: subagent control. tool_set restricts the registry the leaf
    # gets; concurrency=False forces the orchestrator to run the leaf
    # sequentially in-process even when parallel mode is on (e.g. for
    # leaves that need interactive approval).
    tool_set: list[str] | None = None
    concurrency: bool = True
    # Phase 19: multi-skill compose. When set, the orchestrator unions
    # capabilities from every named skill and concatenates their bodies
    # in list order. Takes precedence over `skill` (singular).
    skills: list[str] | None = None


SYSTEM = """You decompose a confirmed user task into a small plan tree.

A plan tree is a JSON object. The root has a goal and zero or more children.
Each child has:
  - id: short kebab-case label, unique among siblings
  - goal: one sentence — what this leaf must accomplish
  - skill: name of an installed skill that fits, or null
  - deps: list of sibling ids that must complete first (default empty)
  - tool_set (OPTIONAL): list of tool names this leaf is allowed to call
      (e.g. ["fs_read", "fs_list", "web_fetch"]). Omit or null = the full
      default registry. For research-only leaves, prefer the read-only set.
  - concurrency (OPTIONAL): true (default) to allow this leaf to run in a
      parallel subagent, false to force sequential in-process execution.
      Set to false for any leaf that may need interactive approval.

RULES:
  - Bias toward fewer leaves. 1 leaf is the right answer for simple tasks.
  - Never more than {fanout} children at a node.
  - Never deeper than {depth} (root is depth 0; leaves at depth {depth_minus_one}).
  - Each leaf must be completable in roughly {leaf_steps} tool calls.
  - Use deps for genuine ordering. Avoid serializing for serializing's sake.

If the task is small enough for one leaf, return:
  {{"goal": "...", "children": [{{"id": "main", "goal": "...", "skill": null, "deps": []}}]}}

Available skills (informational; skill is OPTIONAL on each leaf):
{skills_summary}

Return STRICT JSON. No prose, no fences."""


def plan(
    chosen_action: str,
    available_skills: list[str] | None = None,
    *,
    temperature: float = 0.4,
) -> PlanNode:
    """Ask the LLM for a plan tree. Returns a PlanNode (root)."""
    skills_summary = "\n".join(f"  - {s}" for s in (available_skills or [])) or "  (none)"
    system = SYSTEM.format(
        fanout=config.PLAN_MAX_FANOUT,
        depth=config.PLAN_MAX_DEPTH,
        depth_minus_one=config.PLAN_MAX_DEPTH - 1,
        leaf_steps=config.PLAN_LEAF_STEPS,
        skills_summary=skills_summary,
    )
    msg = llm.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": chosen_action},
        ],
        json_mode=True,
        temperature=temperature,
    )
    data = llm.parse_json_loose(msg.get("content") or "{}")
    return _coerce_tree(data, depth=0)


def _coerce_tree(d: Any, *, depth: int) -> PlanNode:
    if not isinstance(d, dict):
        return PlanNode(id="main", goal="(empty plan)", deps=[])
    goal = str(d.get("goal", "")).strip() or "(unspecified)"
    raw_tool_set = d.get("tool_set")
    tool_set: list[str] | None
    if isinstance(raw_tool_set, list):
        tool_set = [str(x) for x in raw_tool_set]
    else:
        tool_set = None
    raw_concurrency = d.get("concurrency", True)
    concurrency = bool(raw_concurrency) if raw_concurrency is not None else True
    raw_skills = d.get("skills")
    skills_list: list[str] | None
    if isinstance(raw_skills, list) and raw_skills:
        skills_list = [str(x) for x in raw_skills if x]
    else:
        skills_list = None
    node = PlanNode(
        id=str(d.get("id", "root")),
        goal=goal,
        skill=(d.get("skill") or None),
        deps=list(d.get("deps") or []),
        tool_set=tool_set,
        concurrency=concurrency,
        skills=skills_list,
    )
    if depth >= config.PLAN_MAX_DEPTH:
        return node
    raw_children = d.get("children") or []
    if not isinstance(raw_children, list):
        return node
    for child in raw_children[: config.PLAN_MAX_FANOUT]:
        node.children.append(_coerce_tree(child, depth=depth + 1))
    return node


def is_trivial(root: PlanNode) -> bool:
    """A plan is trivial when it's a single leaf (no real decomposition)."""
    return not root.children or (
        len(root.children) == 1 and not root.children[0].children
    )


def render(node: PlanNode, *, indent: int = 0) -> str:
    pad = "  " * indent
    skill = f" [skill={node.skill}]" if node.skill else ""
    deps = f" [deps={','.join(node.deps)}]" if node.deps else ""
    head = f"{pad}- {node.id}: {node.goal}{skill}{deps}"
    sub = "\n".join(render(c, indent=indent + 1) for c in node.children)
    return head + ("\n" + sub if sub else "")


def topo_order(children: list[PlanNode]) -> list[PlanNode]:
    """Kahn's algorithm over `deps` (sibling ids)."""
    by_id = {c.id: c for c in children}
    incoming: dict[str, set[str]] = {c.id: set(d for d in c.deps if d in by_id) for c in children}
    out: list[PlanNode] = []
    ready = [cid for cid, deps in incoming.items() if not deps]
    while ready:
        ready.sort()  # deterministic
        cid = ready.pop(0)
        out.append(by_id[cid])
        for other_id, deps in list(incoming.items()):
            if cid in deps:
                deps.remove(cid)
                if not deps and other_id not in [n.id for n in out] and other_id not in ready:
                    ready.append(other_id)
    # If there's a cycle, append the remainder in input order — better than crashing.
    seen = {n.id for n in out}
    for c in children:
        if c.id not in seen:
            out.append(c)
    return out
