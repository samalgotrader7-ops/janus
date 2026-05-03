"""
swarms/runner.py — coordinator that runs a swarm spec.

A swarm fans work across N sub-agents per phase, runs them concurrently,
collapses their outputs via an aggregator, then chains phases in
declaration order (phase B receives phase A's aggregated output).

Sub-agent dispatch uses ThreadPoolExecutor calling
`subagent._run_in_process` directly — bypasses the subprocess `_RUNNER`
default (which is for plan trees). Threads share the LLM module's
connection pool; the GIL is released during HTTP I/O so 5–30 parallel
sub-agents are genuinely concurrent.

For v1.4 phase 3 (this commit): single-phase pattern only. Aggregator
output is the raw list of sub-agent outputs (real aggregators land in
phase 5). Sequential phase chaining via `depends_on` lands in phase 6.
Budget enforcement lands in phase 4. Cancellation in phase 7.
"""

from __future__ import annotations
import json as _json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .. import config, cost, hooks, subagent
from . import aggregators as aggregators_mod
from . import budget as budget_mod
from . import cancel as cancel_mod
from . import recursion as recursion_mod
from . import spec as spec_mod
from . import state


# ---------- Result dataclasses ----------


@dataclass
class SubagentRunSummary:
    agent_id: str
    role: str
    phase_name: str
    output: str
    error: str | None
    trace_step_count: int


@dataclass
class PhaseRunSummary:
    name: str
    aggregated: Any
    sub_agents: list[SubagentRunSummary] = field(default_factory=list)
    error: str | None = None


@dataclass
class SwarmRunResult:
    run_id: str
    spec_name: str
    inputs: dict
    phases: list[PhaseRunSummary] = field(default_factory=list)
    final: Any = None
    error: str | None = None


# ---------- Entry point ----------


def run_swarm(
    spec: spec_mod.Spec,
    *,
    inputs: dict,
    parent_chat_id: str | None = None,
    parent_run_id: str | None = None,
) -> SwarmRunResult:
    """Run a swarm spec end-to-end.

    Validates inputs (raises SpecError on bad input — $0 spent),
    creates the run directory, dispatches each phase, chains aggregated
    outputs, writes final.json.

    v1.4: depth-tracked. The current thread's swarm depth is incremented
    for the lifetime of this call (decremented on exit). When v1.5 lands
    the model-callable swarm.run tool, it'll check depth against
    spec.budget.max_recursion_depth before allowing nested spawn.
    """
    # Refuse nested spawns that would exceed the spec's recursion budget.
    if recursion_mod.exceeds_recursion_depth(spec.budget.max_recursion_depth):
        return SwarmRunResult(
            run_id="",
            spec_name=spec.name,
            inputs={},
            phases=[],
            final=None,
            error=(
                f"recursion_depth_exceeded: current depth "
                f"{recursion_mod.swarm_depth()} >= max "
                f"{spec.budget.max_recursion_depth}"
            ),
        )
    with recursion_mod.depth_scope():
        return _run_swarm_inner(
            spec, inputs=inputs,
            parent_chat_id=parent_chat_id, parent_run_id=parent_run_id,
        )


def _run_swarm_inner(
    spec: spec_mod.Spec,
    *,
    inputs: dict,
    parent_chat_id: str | None,
    parent_run_id: str | None,
) -> SwarmRunResult:
    validated = spec_mod.validate_inputs(spec, inputs)

    # PreSwarmSpawn hook can deny the whole swarm. Fires BEFORE we mint a
    # run id so a denied swarm leaves no on-disk artifacts.
    pre_decision = hooks.fire(
        hooks.PRE_SWARM_SPAWN,
        {
            "spec": spec.name, "spec_version": spec.version,
            "inputs": validated, "parent_chat_id": parent_chat_id,
            "parent_run_id": parent_run_id,
        },
        match_field="spec",
    )
    if not pre_decision.allow:
        return SwarmRunResult(
            run_id="",
            spec_name=spec.name,
            inputs=validated,
            phases=[],
            final=None,
            error=f"hook_denied: {pre_decision.reason or 'PreSwarmSpawn refused'}",
        )

    run_id = state.new_run_id()
    state.init_run_dir(run_id)

    # Freeze the spec for replay/audit.
    if spec.path is not None:
        try:
            state.freeze_spec(run_id, spec.path.read_text(encoding="utf-8"))
        except OSError:
            pass

    state.write_inputs(run_id, validated)
    state.write_metadata(run_id, {
        "started": state._now_iso(),
        "spec_name": spec.name,
        "spec_version": spec.version,
        "parent_chat_id": parent_chat_id,
        "parent_run_id": parent_run_id,
        "default_mode": spec.permissions.default_mode,
        "models_per_role": {
            p.role: p.model for p in spec.phases if p.model
        },
    })
    state.append_timeline(run_id, {
        "type": "swarm_start",
        "spec": spec.name,
        "n_phases": len(spec.phases),
    })

    budget = budget_mod.SwarmBudget(spec.budget)
    token = cancel_mod.CancellationToken(run_id)
    token.start_watcher()
    phase_results: list[PhaseRunSummary] = []
    last_aggregated: Any = validated  # phase 0 input = swarm inputs

    try:
        for i, phase in enumerate(spec.phases):
            # Cooperative cancellation check first — cheaper than budget.
            if token.is_cancelled():
                state.append_timeline(run_id, {
                    "type": "swarm_cancelled",
                    "phase": phase.name, "phase_num": i,
                    "completed_phases": [p.name for p in phase_results],
                })
                state.write_final(run_id, {
                    "error": "cancelled",
                    "completed_phases": [p.name for p in phase_results],
                    "snapshot": budget.snapshot(run_id),
                })
                return SwarmRunResult(
                    run_id=run_id, spec_name=spec.name, inputs=validated,
                    phases=phase_results, final=None,
                    error="cancelled",
                )
            # Budget check BEFORE starting the phase. Two checks:
            # 1. Are current totals already over a cap?
            # 2. Is there room for at least 1 more sub-agent? (No room
            #    means this phase can't produce anything useful — clearer
            #    to kill than to silently run an empty phase.)
            for v in (budget.check(run_id), budget.can_dispatch_n_more(1)):
                if not v.allowed:
                    state.append_timeline(run_id, {
                        "type": "budget_exceeded",
                        "phase": phase.name, "phase_num": i,
                        "reason": v.reason,
                        "snapshot": budget.snapshot(run_id),
                    })
                    state.write_final(run_id, {
                        "error": "budget_exceeded",
                        "reason": v.reason,
                        "snapshot": budget.snapshot(run_id),
                        "completed_phases": [p.name for p in phase_results],
                    })
                    return SwarmRunResult(
                        run_id=run_id, spec_name=spec.name, inputs=validated,
                        phases=phase_results, final=None,
                        error=f"budget_exceeded: {v.reason}",
                    )
            phase_input = _phase_input_for(
                phase, last_aggregated, phase_results,
            )
            state.init_phase_dir(run_id, i, phase.name)
            state.write_phase_input(run_id, i, phase.name, phase_input)
            state.append_timeline(run_id, {
                "type": "phase_start",
                "phase": phase.name, "phase_num": i,
            })

            result = _run_phase(
                spec=spec, phase=phase, phase_num=i,
                phase_input=phase_input, run_id=run_id, budget=budget,
                token=token,
            )
            phase_results.append(result)
            last_aggregated = result.aggregated
            state.write_phase_aggregated(
                run_id, i, phase.name, result.aggregated,
            )
            state.append_timeline(run_id, {
                "type": "phase_done",
                "phase": phase.name, "phase_num": i,
                "n_subagents": len(result.sub_agents),
                "n_errors": sum(1 for s in result.sub_agents if s.error),
                "budget_snapshot": budget.snapshot(run_id),
            })
    except Exception as e:
        state.append_timeline(run_id, {
            "type": "swarm_error", "error": f"{type(e).__name__}: {e}",
        })
        state.write_final(run_id, {"error": f"{type(e).__name__}: {e}"})
        return SwarmRunResult(
            run_id=run_id, spec_name=spec.name, inputs=validated,
            phases=phase_results, final=None,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        token.stop_watcher()

    final = phase_results[-1].aggregated if phase_results else None
    state.write_final(run_id, final)
    state.append_timeline(run_id, {
        "type": "swarm_done",
        "budget_snapshot": budget.snapshot(run_id),
    })

    # PostSwarmComplete hook fires AFTER final.json is written so the
    # hook command can read it. Observation only — no deny semantics.
    hooks.fire(
        hooks.POST_SWARM_COMPLETE,
        {
            "spec": spec.name, "run_id": run_id,
            "n_phases": len(phase_results),
            "n_subagents_total": sum(len(p.sub_agents) for p in phase_results),
            "n_errors_total": sum(
                sum(1 for s in p.sub_agents if s.error) for p in phase_results
            ),
            "budget_snapshot": budget.snapshot(run_id),
        },
        match_field="spec",
    )

    return SwarmRunResult(
        run_id=run_id, spec_name=spec.name, inputs=validated,
        phases=phase_results, final=final,
    )


# ---------- Phase-input resolution ----------


def _phase_input_for(
    phase: spec_mod.Phase,
    last_aggregated: Any,
    prior_results: list[PhaseRunSummary],
) -> Any:
    """Resolve a phase's input.

    - phase[0] (no depends_on, first in list) → swarm inputs
    - phase[i>0] (no depends_on)                → prior phase's aggregated output
    - any phase with `depends_on: <name>`       → that named phase's aggregated output
    """
    if phase.depends_on is None:
        return last_aggregated
    for p in prior_results:
        if p.name == phase.depends_on:
            return p.aggregated
    # Spec validator catches unknown / forward refs; this is unreachable.
    return last_aggregated


# ---------- Phase dispatch ----------


def _run_phase(
    *,
    spec: spec_mod.Spec,
    phase: spec_mod.Phase,
    phase_num: int,
    phase_input: Any,
    run_id: str,
    budget: budget_mod.SwarmBudget,
    token: cancel_mod.CancellationToken,
) -> PhaseRunSummary:
    """Build sub-agent specs, dispatch concurrently, collect results,
    aggregate.

    Budget-aware: refuses to dispatch if max_subagents would be exceeded;
    re-checks after each return; writes a timeline event when killed
    mid-phase. Returns a partial PhaseRunSummary on kill.

    Cancel-aware: passes the cancel_event into each sub-agent's executor
    so currently-running sub-agents exit between steps when cancelled."""
    tasks = _partition(phase, phase_input)

    # Pre-flight subagent-count check.
    pre = budget.can_dispatch_n_more(len(tasks))
    if not pre.allowed:
        # Cap the dispatched count at what the budget still allows.
        with budget.state._lock:
            already = budget.state.n_subagents_dispatched
        room = max(0, budget.budget.max_subagents - already)
        tasks = tasks[:room]
        state.append_timeline(run_id, {
            "type": "phase_truncated",
            "phase": phase.name, "reason": pre.reason,
            "kept": len(tasks),
        })

    specs: list[tuple[str, subagent.SubagentSpec]] = []
    for idx, task in enumerate(tasks):
        agent_id = state.new_agent_id(phase.role, idx)
        body = _format_body(spec, phase, agent_id, task)
        sub_spec = subagent.SubagentSpec(
            leaf_id=agent_id,
            parent_id=run_id,
            description=f"{spec.name}/{phase.name}/{phase.role}#{idx}",
            request=body,
            label=f"{spec.name} :: {phase.name} :: {agent_id}",
            action=body,
            skill_body="",
            memory_preamble="",
            tool_names=phase.tool_names if phase.tool_names else None,
            capability_set=dict(phase.capabilities) if phase.capabilities else None,
            model=phase.model,
        )
        specs.append((agent_id, sub_spec))

    state.append_timeline(run_id, {
        "type": "phase_dispatch",
        "phase": phase.name, "n_subagents": len(specs),
    })

    n = max(1, min(len(specs), config.SWARM_DEFAULT_CONCURRENCY))
    results: dict[str, subagent.SubagentResult] = {}

    def _run_one(agent_id: str, sub_spec: subagent.SubagentSpec) -> None:
        # PreSubagentSpawn hook can deny this dispatch (allows fine-grained
        # gating, e.g., "no spawns to model X" or "no spawns for role Y").
        pre = hooks.fire(
            hooks.PRE_SUBAGENT_SPAWN,
            {
                "spec": spec.name, "run_id": run_id,
                "agent_id": agent_id, "role": phase.role,
                "phase": phase.name, "model": phase.model,
            },
            match_field="role",
        )
        if not pre.allow:
            results[agent_id] = subagent.SubagentResult(
                leaf_id=agent_id, parent_id=run_id,
                output="", trace=[],
                error=f"hook_denied: {pre.reason or 'PreSubagentSpawn refused'}",
            )
            state.append_timeline(run_id, {
                "type": "subagent_denied",
                "phase": phase.name, "agent_id": agent_id,
                "reason": pre.reason,
            })
            return

        # Mark this thread for cost attribution. Set BEFORE dispatch so
        # llm.chat()'s cost.record() call sees the right context.
        cost.set_active_subagent(
            swarm_run_id=run_id,
            agent_id=agent_id,
            role=phase.role,
            phase=phase.name,
        )
        budget.register_dispatch()
        state.append_timeline(run_id, {
            "type": "subagent_start",
            "phase": phase.name, "agent_id": agent_id, "role": phase.role,
        })
        try:
            result = subagent._run_in_process(
                sub_spec, cancel_event=token.event,
            )
        except Exception as e:
            result = subagent.SubagentResult(
                leaf_id=agent_id, parent_id=run_id,
                output="", trace=[],
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            # Count tool calls (exclude the final-text record).
            tool_call_count = sum(
                1 for r in (result.trace or []) if r.get("type") == "tool_call"
            )
            budget.register_complete(tool_call_count)
            cost.clear_active_subagent()
        results[agent_id] = result
        state.write_agent_transcript(
            run_id, phase_num, phase.name, agent_id, result.trace or [],
        )
        state.append_timeline(run_id, {
            "type": "subagent_done",
            "phase": phase.name, "agent_id": agent_id,
            "error": result.error,
            "tool_calls": tool_call_count,
        })
        # PostSubagentComplete fires AFTER the transcript is on disk so
        # the hook command can read it. Observation only.
        hooks.fire(
            hooks.POST_SUBAGENT_COMPLETE,
            {
                "spec": spec.name, "run_id": run_id,
                "agent_id": agent_id, "role": phase.role,
                "phase": phase.name, "error": result.error,
                "output_chars": len(result.output or ""),
                "tool_calls": tool_call_count,
            },
            match_field="role",
        )

    if len(specs) == 0:
        # Empty phase — nothing to dispatch. Return empty aggregated.
        return PhaseRunSummary(name=phase.name, aggregated=[])

    if n == 1 or len(specs) == 1:
        for agent_id, sub_spec in specs:
            _run_one(agent_id, sub_spec)
    else:
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_run_one, aid, s) for aid, s in specs]
            for f in futures:
                f.result()  # propagate any unhandled exception

    # Build per-sub-agent summaries in deterministic input order.
    summaries: list[SubagentRunSummary] = []
    for agent_id, _ in specs:
        r = results.get(agent_id)
        summaries.append(SubagentRunSummary(
            agent_id=agent_id,
            role=phase.role,
            phase_name=phase.name,
            output=r.output if r else "",
            error=r.error if r else "no_result",
            trace_step_count=len(r.trace) if r and r.trace else 0,
        ))

    # Phase 5: dispatch real aggregator. Errors filtered out; aggregator
    # exception caught and recorded as the phase's aggregated output so
    # the swarm can still finish (the aggregator is fallible by design —
    # llm_summarize hits the network, deterministic ones may raise on
    # malformed sub-agent JSON).
    non_error = [s.output for s in summaries if not s.error and s.output]
    try:
        aggregated = aggregators_mod.aggregate(
            phase.aggregator,
            non_error,
            phase.aggregator_args,
            phase_input,
            model=phase.model,
            phase_name=phase.name,
        )
        agg_error: str | None = None
    except Exception as e:
        aggregated = {"error": f"aggregator_failed: {type(e).__name__}: {e}"}
        agg_error = f"aggregator_failed: {type(e).__name__}: {e}"

    return PhaseRunSummary(
        name=phase.name,
        aggregated=aggregated,
        sub_agents=summaries,
        error=agg_error,
    )


# ---------- Input partitioning ----------


def _partition(phase: spec_mod.Phase, phase_input: Any) -> list[Any]:
    """Split phase input into N tasks per the phase's input_partition.

    - pattern=single             → one task with the entire input (regardless of partition)
    - pattern=map_reduce, full   → one task with the entire input
    - pattern=map_reduce, per_item: input must be a list; one task per item,
                                    capped at max_per_batch tasks
    - pattern=map_reduce, regional_batches: input must be a list; chunk into
                                            roughly equal batches up to max_per_batch
    """
    if phase.pattern == "single":
        return [phase_input]

    if phase.input_partition == "full":
        return [phase_input]

    if not isinstance(phase_input, list):
        # Non-list input under a list-expecting partition: degrade to one task.
        return [phase_input]

    if phase.input_partition == "per_item":
        return phase_input[:phase.max_per_batch]

    if phase.input_partition == "regional_batches":
        if not phase_input:
            return []
        n_batches = min(phase.max_per_batch, len(phase_input))
        batch_size = max(1, (len(phase_input) + n_batches - 1) // n_batches)
        out: list[Any] = []
        for i in range(0, len(phase_input), batch_size):
            out.append(phase_input[i:i + batch_size])
            if len(out) >= phase.max_per_batch:
                break
        return out

    return [phase_input]


# ---------- Body interpolation ----------


def _format_body(
    spec: spec_mod.Spec,
    phase: spec_mod.Phase,
    agent_id: str,
    task_input: Any,
) -> str:
    """Render the spec body with {role}, {phase}, {input}, {spec_name},
    {agent_id} placeholders interpolated. Uses .replace (NOT .format)
    so user content with literal braces doesn't blow up."""
    body = spec.body
    if isinstance(task_input, str):
        input_str = task_input
    else:
        try:
            input_str = _json.dumps(task_input, default=str)
        except (TypeError, ValueError):
            input_str = repr(task_input)
    placeholders = {
        "{role}": phase.role,
        "{phase}": phase.name,
        "{spec_name}": spec.name,
        "{agent_id}": agent_id,
        "{input}": input_str,
    }
    for k, v in placeholders.items():
        body = body.replace(k, v)
    return body
