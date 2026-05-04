"""
tools/swarm_run.py — model-callable swarm.run tool (v1.5 phase 6).

The model can call this from inside its own loop to spawn a swarm by
spec name. Recursion is bounded by the spec's own
budget.max_recursion_depth (the threading.local depth scope from v1.4
phase 8 already enforces this — exceeding the depth returns a
RecursionError-flavored result, not a crash).

WHY EXPOSE THIS TO THE MODEL:
The "12-hour unattended" use case Sam described needs the model to
orchestrate work without a human in the loop. The model decides "I
should split this 1500-row scrape into 5 parallel batches" and calls
swarm.run('data-scrape', inputs={'rows': N}). The swarm runs to
completion; the model gets back a summary + run_id for follow-up.

SAFETY:
- spec_name must reference an existing spec under
  ~/.janus/swarms/specs/<name>.md (no inline specs from the model;
  user has already vetted the spec by writing it)
- spec inputs are validated against the spec's input schema
- spec budget caps apply (v1.4 phase 4 budget.SwarmBudget kill switch)
- recursion depth tracked via threading.local (v1.4 phase 8)
- in auto mode, the spec.runner inherits auto behavior — sub-agents
  themselves get the safety analyzer
- the tool itself is risk='exec' so default mode asks before each spawn

OUTPUT FORMAT:
The model gets back a one-page summary: run_id + per-phase counts +
final.json path. Full results live on disk; the model can fs_read the
final.json or query via swarm.status (separate tool, future) for detail.
"""

from __future__ import annotations
import json
from typing import Any

from .base import Tool


_DESC = (
    "Spawn a Janus swarm by spec name. Phases run sequentially; "
    "sub-agents within a phase run in parallel. Returns run_id + summary. "
    "Use only when a task needs PARALLEL sub-work; for a single-thread "
    "task, just continue the current loop. Spec must exist at "
    "~/.janus/swarms/specs/<name>.md."
)


class SwarmRun(Tool):
    name = "swarm_run"
    description = _DESC
    parameters = {
        "type": "object",
        "properties": {
            "spec_name": {
                "type": "string",
                "description": (
                    "kebab-case name of a spec under "
                    "~/.janus/swarms/specs/<spec_name>.md"
                ),
            },
            "inputs": {
                "type": "object",
                "description": (
                    "Launch inputs as a JSON object; validated against "
                    "the spec's inputs schema before any sub-agent spawns "
                    "($0 spent on bad inputs)"
                ),
            },
        },
        "required": ["spec_name"],
    }
    risk = "exec"

    def run(self, args: dict, approver) -> str:
        spec_name = args.get("spec_name") or ""
        inputs = args.get("inputs") or {}
        if not isinstance(spec_name, str) or not spec_name.strip():
            return "error: spec_name required (kebab-case)"
        if not isinstance(inputs, dict):
            return f"error: inputs must be a JSON object, got {type(inputs).__name__}"

        # Capability/approval gate. We pass spec_name + inputs into the
        # approver so auto-mode + the user can decide whether to allow
        # this spawn.
        if not approver(
            f"swarm.run({spec_name})",
            json.dumps(inputs)[:200],
            capability=("swarm", "run", spec_name),
        ):
            return f"refused: swarm.run({spec_name}) not approved"

        # Lazy import to avoid pulling the swarms package at module-load
        # time (keeps tools package import-cheap).
        from .. import swarms

        spec = swarms.spec.find_spec(spec_name)
        if spec is None:
            return (
                f"error: no spec named {spec_name!r}. "
                f"Available: {[s.name for s in swarms.spec.list_specs()]}"
            )

        try:
            result = swarms.runner.run_swarm(spec, inputs=inputs)
        except swarms.spec.SpecError as e:
            return f"input validation failed: {e}"
        except Exception as e:
            return f"swarm crashed: {type(e).__name__}: {e}"

        if result.error:
            return _format_summary(result, error=True)
        return _format_summary(result)


def _format_summary(result: Any, *, error: bool = False) -> str:
    """One-page text summary the model can read in its next turn."""
    lines: list[str] = []
    lines.append(f"run_id: {result.run_id}")
    lines.append(f"spec:   {result.spec_name}")
    if error:
        lines.append(f"error:  {result.error}")
    lines.append(f"phases: {len(result.phases)}")
    for p in result.phases:
        n_err = sum(1 for s in p.sub_agents if s.error)
        lines.append(
            f"  {p.name:<20}  sub-agents={len(p.sub_agents)}  "
            f"errors={n_err}"
        )
    if result.final is not None and not error:
        # Truncate the final blob; the model can fs_read final.json for full.
        try:
            final_str = json.dumps(result.final, default=str)
        except Exception:
            final_str = str(result.final)
        if len(final_str) > 500:
            lines.append(f"final (preview): {final_str[:500]}…")
            lines.append(
                f"  full at: ~/.janus/swarms/runs/{result.run_id}/final.json"
            )
        else:
            lines.append(f"final: {final_str}")
    return "\n".join(lines)
