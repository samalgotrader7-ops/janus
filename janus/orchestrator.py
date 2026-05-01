"""
orchestrator.py — Phase 4: walk a plan tree, run each leaf with the executor.
                — Phase 8: optional parallel mode via subagent subprocesses.

DESIGN:
  - Trivial plan (one leaf, no children) → identical to Phase 1-3 linear
    execute. Same prompt, same budget. No regression.
  - Non-trivial plan, parallel=False (default) → topological walk over
    children sequentially, each leaf in-process.
  - Non-trivial plan, parallel=True (Phase 8) → walk in dependency waves;
    leaves with concurrency=True run via subagent subprocesses, capped by
    JANUS_SUBAGENT_CONCURRENCY and serialized when their fs.write
    capabilities overlap. Leaves with concurrency=False stay in-process.

WE DO NOT:
  - Allow a leaf to spawn its own plan (no recursive planning). The planner
    runs once at the top.
  - Allow a subagent to spawn its own subagent. The recursion guard in
    `subagent.is_subagent_env()` makes parallel=True a no-op when this
    process is itself a subagent.
  - Increase MAX_STEPS — we INSTEAD give each leaf its own bounded budget,
    so a 4-leaf plan effectively gets 4 × PLAN_LEAF_STEPS work.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable

from . import config, executor, llm, skills as skills_mod, subagent
from .planner import PlanNode, topo_order
from .tools import default_registry, make_capability_aware, CapabilitySet


@dataclass
class LeafResult:
    id: str
    goal: str
    output: str
    trace: list[dict]
    skill: str | None = None
    error: str | None = None


@dataclass
class RunResult:
    final_output: str
    leaves: list[LeafResult] = field(default_factory=list)
    plan_was_trivial: bool = False


def run(
    *,
    original_request: str,
    chosen_label: str,
    chosen_action: str,
    plan: PlanNode,
    base_approver: Callable[..., bool],
    on_step: Callable[[dict], None] | None = None,
    on_leaf_start: Callable[[PlanNode], None] | None = None,
    on_leaf_done: Callable[[LeafResult], None] | None = None,
    memory_preamble: str = "",
    attached_skill: "skills_mod.Skill | None" = None,
    parallel: bool = False,
    parent_id: str = "",
) -> RunResult:
    """Walk plan; return RunResult with per-leaf detail and a final summary.

    `parallel`: Phase 8. When True AND we are not ourselves a subagent,
        leaves with `concurrency=True` run via subagent subprocesses up to
        JANUS_SUBAGENT_CONCURRENCY in parallel. Leaves with
        `concurrency=False` always run sequentially in-process. Same-file
        write conflicts (overlapping fs.write globs) serialize within a
        wave. Defaults to False to preserve Phase 4 behavior + existing
        tests.
    `parent_id`: opaque string the subagent records as `parent_id` in its
        own log entry. Lets the audit trail link parent ↔ children.
    """
    # Trivial plans: defer to executor.execute() with the chosen interpretation.
    is_trivial = not plan.children or (
        len(plan.children) == 1 and not plan.children[0].children
    )
    if is_trivial:
        return _run_linear(
            original_request=original_request,
            chosen_label=chosen_label,
            chosen_action=chosen_action,
            base_approver=base_approver,
            on_step=on_step,
            memory_preamble=memory_preamble,
            attached_skill=attached_skill,
        )

    use_parallel = parallel and not subagent.is_subagent_env()

    leaf_temp_steps = _override_max_steps(config.PLAN_LEAF_STEPS)
    try:
        if use_parallel:
            leaves = _run_parallel(
                plan=plan,
                original_request=original_request,
                chosen_label=chosen_label,
                base_approver=base_approver,
                on_step=on_step,
                on_leaf_start=on_leaf_start,
                on_leaf_done=on_leaf_done,
                memory_preamble=memory_preamble,
                attached_skill=attached_skill,
                parent_id=parent_id,
            )
        else:
            leaves = _run_sequential(
                plan=plan,
                original_request=original_request,
                chosen_label=chosen_label,
                base_approver=base_approver,
                on_step=on_step,
                on_leaf_start=on_leaf_start,
                on_leaf_done=on_leaf_done,
                memory_preamble=memory_preamble,
                attached_skill=attached_skill,
            )
    finally:
        _restore_max_steps(leaf_temp_steps)

    final = _summarize(plan.goal, leaves)
    return RunResult(final_output=final, leaves=leaves, plan_was_trivial=False)


def _run_sequential(
    *,
    plan: PlanNode,
    original_request: str,
    chosen_label: str,
    base_approver,
    on_step,
    on_leaf_start,
    on_leaf_done,
    memory_preamble: str,
    attached_skill: "skills_mod.Skill | None",
) -> list[LeafResult]:
    leaves: list[LeafResult] = []
    by_id: dict[str, LeafResult] = {}

    for leaf in topo_order(plan.children):
        if on_leaf_start:
            on_leaf_start(leaf)

        leaf_skill = (
            _resolve_leaf_skill(leaf, attached_skill)
        )
        caps = leaf_skill.capabilities if leaf_skill else CapabilitySet()
        tools = default_registry(
            capabilities=caps,
            tool_names=leaf.tool_set,
        )
        approver = make_capability_aware(base_approver, caps)

        sibling_context = _format_sibling_context(leaf, by_id)
        leaf_action = leaf.goal + (
            f"\n\nResults from prior steps:\n{sibling_context}"
            if sibling_context else ""
        )
        try:
            output, trace = executor.execute(
                original_request=original_request,
                chosen_label=f"{chosen_label} :: {leaf.id}",
                chosen_action=leaf_action,
                tools=tools,
                approver=approver,
                on_step=on_step,
                skill_body=(leaf_skill.body if leaf_skill else ""),
                memory_preamble=memory_preamble,
            )
            lr = LeafResult(id=leaf.id, goal=leaf.goal, output=output,
                            trace=trace,
                            skill=(leaf_skill.name if leaf_skill else None))
        except Exception as e:
            lr = LeafResult(id=leaf.id, goal=leaf.goal, output="",
                            trace=[], error=f"{type(e).__name__}: {e}",
                            skill=(leaf_skill.name if leaf_skill else None))
        leaves.append(lr)
        by_id[lr.id] = lr
        if on_leaf_done:
            on_leaf_done(lr)
    return leaves


def _run_parallel(
    *,
    plan: PlanNode,
    original_request: str,
    chosen_label: str,
    base_approver,
    on_step,
    on_leaf_start,
    on_leaf_done,
    memory_preamble: str,
    attached_skill: "skills_mod.Skill | None",
    parent_id: str,
) -> list[LeafResult]:
    """Wave-by-wave parallel walk. Within each wave:
      - concurrency=False leaves run in-process, sequentially.
      - concurrency=True leaves run via subagent.run_batch, conflict-serialized.
    """
    by_id: dict[str, LeafResult] = {}
    out: list[LeafResult] = []

    for wave in _waves(plan.children):
        # Sequential leaves first (they may have side effects parallel leaves
        # need to see).
        sequential = [n for n in wave if not n.concurrency]
        parallel_pool = [n for n in wave if n.concurrency]

        for leaf in sequential:
            lr = _run_one_inprocess(
                leaf=leaf,
                original_request=original_request,
                chosen_label=chosen_label,
                base_approver=base_approver,
                on_step=on_step,
                on_leaf_start=on_leaf_start,
                memory_preamble=memory_preamble,
                attached_skill=attached_skill,
                by_id=by_id,
            )
            by_id[lr.id] = lr
            out.append(lr)
            if on_leaf_done:
                on_leaf_done(lr)

        # Parallel leaves: pack into conflict-free batches up to the cap.
        leaf_caps: dict[str, dict | None] = {}
        for n in parallel_pool:
            leaf_skill = (
                skills_mod.load(n.skill) if n.skill else attached_skill
            )
            leaf_caps[n.id] = (
                _caps_to_dict(leaf_skill.capabilities) if leaf_skill else None
            )

        remaining = list(parallel_pool)
        cap_n = config.SUBAGENT_CONCURRENCY
        while remaining:
            batch: list[PlanNode] = []
            skipped: list[PlanNode] = []
            for n in remaining:
                if len(batch) >= cap_n:
                    skipped.append(n)
                    continue
                if any(
                    subagent.specs_conflict(leaf_caps[n.id], leaf_caps[b.id])
                    for b in batch
                ):
                    skipped.append(n)
                    continue
                batch.append(n)

            for leaf in batch:
                if on_leaf_start:
                    on_leaf_start(leaf)

            specs = []
            for leaf in batch:
                leaf_skill = (
                    _resolve_leaf_skill(leaf, attached_skill)
                )
                sibling_context = _format_sibling_context(leaf, by_id)
                leaf_action = leaf.goal + (
                    f"\n\nResults from prior steps:\n{sibling_context}"
                    if sibling_context else ""
                )
                specs.append(subagent.SubagentSpec(
                    leaf_id=leaf.id,
                    parent_id=parent_id,
                    description=f"{chosen_label} :: {leaf.id}",
                    request=original_request,
                    label=f"{chosen_label} :: {leaf.id}",
                    action=leaf_action,
                    skill_body=(leaf_skill.body if leaf_skill else ""),
                    memory_preamble=memory_preamble,
                    tool_names=leaf.tool_set,
                    capability_set=leaf_caps[leaf.id],
                ))

            results = subagent.run_batch(specs, concurrency=cap_n)
            results_by_id = {r.leaf_id: r for r in results}

            # Sort by leaf id (Phase 8 design: deterministic logs).
            for leaf in sorted(batch, key=lambda n: n.id):
                r = results_by_id.get(leaf.id)
                leaf_skill = (
                    _resolve_leaf_skill(leaf, attached_skill)
                )
                if r is None:
                    lr = LeafResult(
                        id=leaf.id, goal=leaf.goal, output="", trace=[],
                        error="subagent_returned_no_result",
                        skill=(leaf_skill.name if leaf_skill else None),
                    )
                else:
                    lr = LeafResult(
                        id=leaf.id, goal=leaf.goal,
                        output=r.output, trace=r.trace, error=r.error,
                        skill=(leaf_skill.name if leaf_skill else None),
                    )
                by_id[lr.id] = lr
                out.append(lr)
                if on_leaf_done:
                    on_leaf_done(lr)

            remaining = skipped

    # Final out is in wave-then-batch-then-leaf-id order. Stable sort by id
    # for the final leaf list so the audit trail is fully id-deterministic.
    out.sort(key=lambda lr: lr.id)
    return out


def _run_one_inprocess(
    *,
    leaf: PlanNode,
    original_request: str,
    chosen_label: str,
    base_approver,
    on_step,
    on_leaf_start,
    memory_preamble: str,
    attached_skill: "skills_mod.Skill | None",
    by_id: dict[str, "LeafResult"],
) -> "LeafResult":
    if on_leaf_start:
        on_leaf_start(leaf)
    leaf_skill = (
        _resolve_leaf_skill(leaf, attached_skill)
    )
    caps = leaf_skill.capabilities if leaf_skill else CapabilitySet()
    tools = default_registry(capabilities=caps, tool_names=leaf.tool_set)
    approver = make_capability_aware(base_approver, caps)
    sibling_context = _format_sibling_context(leaf, by_id)
    leaf_action = leaf.goal + (
        f"\n\nResults from prior steps:\n{sibling_context}"
        if sibling_context else ""
    )
    try:
        output, trace = executor.execute(
            original_request=original_request,
            chosen_label=f"{chosen_label} :: {leaf.id}",
            chosen_action=leaf_action,
            tools=tools,
            approver=approver,
            on_step=on_step,
            skill_body=(leaf_skill.body if leaf_skill else ""),
            memory_preamble=memory_preamble,
        )
        return LeafResult(
            id=leaf.id, goal=leaf.goal, output=output, trace=trace,
            skill=(leaf_skill.name if leaf_skill else None),
        )
    except Exception as e:
        return LeafResult(
            id=leaf.id, goal=leaf.goal, output="", trace=[],
            error=f"{type(e).__name__}: {e}",
            skill=(leaf_skill.name if leaf_skill else None),
        )


def _resolve_leaf_skill(leaf: PlanNode, attached: "skills_mod.Skill | None"):
    """Return the leaf's effective skill view.

    Phase 19 multi-skill compose: when `leaf.skills` (plural) is set, we
    union capabilities across all named skills and concatenate their
    bodies in list order. Returns a SYNTHETIC Skill — not a path-backed
    one — so this never mutates persisted skill files.

    Resolution order:
      1. leaf.skills (plural) — compose multiple
      2. leaf.skill (singular) — load one
      3. attached_skill — fall back to the session's attached skill
    """
    # 1. Compose.
    if leaf.skills:
        loaded = []
        for name in leaf.skills:
            s = skills_mod.load(name)
            if s is not None:
                loaded.append(s)
        if loaded:
            merged_caps_list: list = []
            seen = set()
            for s in loaded:
                for c in s.capabilities.caps:
                    key = (c.tool, c.verb, tuple(c.globs))
                    if key not in seen:
                        seen.add(key)
                        merged_caps_list.append(c)
            merged_body = "\n\n---\n\n".join(
                f"# Skill: {s.name}\n{s.body.strip()}" for s in loaded
            )
            # Synthetic skill — same dataclass shape, no path persistence.
            from .tools.capabilities import CapabilitySet as _CS
            return skills_mod.Skill(
                name=("+".join(s.name for s in loaded))[:64],
                description=" + ".join(s.description for s in loaded)[:1024],
                state="quarantined",
                capabilities=_CS(caps=merged_caps_list),
                body=merged_body,
                path=loaded[0].path,
                raw_frontmatter={},
                created="",
                last_promoted=None,
                runs=0,
            )
    # 2. Singular.
    if leaf.skill:
        single = skills_mod.load(leaf.skill)
        if single is not None:
            return single
    # 3. Fallback.
    return attached


def _waves(children: list[PlanNode]) -> list[list[PlanNode]]:
    """Group leaves into topological waves (Kahn rounds)."""
    by_id = {c.id: c for c in children}
    incoming: dict[str, set[str]] = {
        c.id: set(d for d in c.deps if d in by_id) for c in children
    }
    waves: list[list[PlanNode]] = []
    while incoming:
        ready = sorted(cid for cid, deps in incoming.items() if not deps)
        if not ready:
            # Cycle — emit the rest as one wave (don't deadlock).
            ready = sorted(incoming)
        waves.append([by_id[cid] for cid in ready])
        for cid in ready:
            incoming.pop(cid, None)
        for cid in ready:
            for other in incoming.values():
                other.discard(cid)
    return waves


def _caps_to_dict(caps: "CapabilitySet") -> dict:
    return {f"{c.tool}.{c.verb}": list(c.globs) for c in caps.caps}


def _run_linear(*, original_request, chosen_label, chosen_action,
                base_approver, on_step, memory_preamble,
                attached_skill) -> RunResult:
    caps = attached_skill.capabilities if attached_skill else CapabilitySet()
    tools = default_registry(capabilities=caps)
    approver = make_capability_aware(base_approver, caps)
    output, trace = executor.execute(
        original_request=original_request,
        chosen_label=chosen_label,
        chosen_action=chosen_action,
        tools=tools,
        approver=approver,
        on_step=on_step,
        skill_body=(attached_skill.body if attached_skill else ""),
        memory_preamble=memory_preamble,
    )
    leaf = LeafResult(id="main", goal=chosen_action, output=output,
                      trace=trace,
                      skill=(attached_skill.name if attached_skill else None))
    return RunResult(final_output=output, leaves=[leaf], plan_was_trivial=True)


# ---------- helpers ----------


def _format_sibling_context(leaf: PlanNode, by_id: dict[str, LeafResult]) -> str:
    chunks: list[str] = []
    for dep_id in leaf.deps:
        dep = by_id.get(dep_id)
        if not dep:
            continue
        head = (dep.output or dep.error or "").splitlines()
        chunks.append(f"## [{dep.id}] {dep.goal}\n" + "\n".join(head[:30]))
    return "\n\n".join(chunks)


def _summarize(root_goal: str, leaves: list[LeafResult]) -> str:
    """One LLM call to wrap up. Cheap; bounded by leaf count."""
    if not leaves:
        return "(plan produced no leaves)"
    excerpt = "\n\n".join(
        f"## [{lr.id}] {lr.goal}\n"
        f"{(lr.error or lr.output or '(no output)')[:1500]}"
        for lr in leaves
    )
    msg = llm.chat(
        messages=[
            {"role": "system", "content":
                "Summarize the multi-step run for the user. "
                "Be concrete: what was produced, what failed, what's left. "
                "No preamble. Markdown OK. <300 words."},
            {"role": "user", "content":
                f"Goal: {root_goal}\n\nLeaf outputs:\n{excerpt}"},
        ],
        temperature=0.3,
    )
    return (msg.get("content") or "").strip() or "(summary failed)"


# Override MAX_STEPS for the duration of a plan-tree walk.
def _override_max_steps(new_steps: int) -> int:
    prev = config.MAX_STEPS
    config.MAX_STEPS = new_steps
    return prev


def _restore_max_steps(prev: int) -> None:
    config.MAX_STEPS = prev
