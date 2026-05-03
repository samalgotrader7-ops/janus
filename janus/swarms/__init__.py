"""
swarms — v1.4: parallel sub-agent coordination driven by markdown specs.

A swarm is a coordinator that fans work across N sub-agents per phase, runs
them concurrently, aggregates each phase's outputs, and chains phases
sequentially (phase B receives phase A's aggregated output).

Sub-agents reuse `subagent._run_in_process` from Phase 8 — same in-process
executor invocation, same capability isolation, same recursion guard. The
swarm layer adds: spec parsing, phase loop, aggregator dispatch, budget
enforcement, cancellation, per-agent traceability, retry/backoff at LLM
boundary.

Public API:
  spec.load_spec(path)              # parse a swarm spec from disk
  spec.list_specs()                 # all specs in ~/.janus/swarms/specs/
  spec.find_spec(name)              # one spec by name or None
  spec.validate_inputs(spec, dict)  # validate launch inputs against schema

Phases beyond v1.4 land their public API here as they're built.
"""

from . import aggregators, budget, cancel, recursion, runner, slash, spec, state

__all__ = [
    "aggregators", "budget", "cancel", "recursion",
    "runner", "slash", "spec", "state",
]
