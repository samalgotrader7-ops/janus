"""
subagent.py — Phase 8: isolated executors with capability-only approval.

WHY:
- Context isolation: a side task that produces 50 KB of search results
  should not poison the main conversation. The subagent runs in its own
  context and returns only the final output + trace.
- Parallelism: independent leaves of a plan tree can run concurrently up
  to JANUS_SUBAGENT_CONCURRENCY (default 4).

DESIGN:
- A subagent is spawned as a Python subprocess (`python -m janus.subagent`).
  The child sets `JANUS_IS_SUBAGENT=1` so the recursion guard in
  `is_subagent_env()` blocks any further subagent spawning. The build guide
  Phase 8 acceptance criterion #5 forbids subagent recursion verbatim.
- The child reads a SubagentSpec on stdin, runs `executor.execute` against
  a restricted Registry (filtered by `tool_names`) and a capability-only
  approver (interactive prompts are not available — anything outside the
  capability_set is denied), and writes a SubagentResult on stdout.
- For tests, `_RUNNER` indirection swaps the subprocess path for an
  in-process runner so we avoid subprocess + LLM overhead.

NO INTERACTIVE APPROVAL IN SUBAGENTS:
The subagent has no stdin connection to the user; the parent owns the
TTY. So the subagent's approver is `make_capability_aware(deny_approver,
caps)` — capability-granted actions auto-approve, everything else is
denied. This forces the planner to declare per-leaf capabilities up
front; ad-hoc escalations cannot happen mid-leaf.

SAME-FILE CONFLICT DETECTION:
The orchestrator inspects each leaf's `fs.write` capability globs before
batching. Two leaves whose globs overlap must serialize. The check is
intentionally conservative — when in doubt we serialize, never run.
"""

from __future__ import annotations
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import config, executor, logger
from .tools import default_registry, make_capability_aware
from .tools.capabilities import CapabilitySet


# ---------- Spec / Result ----------


@dataclass
class SubagentSpec:
    leaf_id: str
    parent_id: str               # ts of the parent record (for log linkage)
    description: str             # short human-readable label
    request: str
    label: str
    action: str
    skill_body: str = ""
    memory_preamble: str = ""
    tool_names: list[str] | None = None
    capability_set: dict | None = None   # serialized {tool.verb: [globs]}
    model: str | None = None             # v1.4: per-sub-agent model override

    def to_json(self) -> str:
        return json.dumps({
            "leaf_id": self.leaf_id,
            "parent_id": self.parent_id,
            "description": self.description,
            "request": self.request,
            "label": self.label,
            "action": self.action,
            "skill_body": self.skill_body,
            "memory_preamble": self.memory_preamble,
            "tool_names": self.tool_names,
            "capability_set": self.capability_set,
            "model": self.model,
        })

    @classmethod
    def from_json(cls, blob: str) -> "SubagentSpec":
        d = json.loads(blob)
        return cls(
            leaf_id=str(d.get("leaf_id", "")),
            parent_id=str(d.get("parent_id", "")),
            description=str(d.get("description", "")),
            request=str(d.get("request", "")),
            label=str(d.get("label", "")),
            action=str(d.get("action", "")),
            skill_body=str(d.get("skill_body", "")),
            memory_preamble=str(d.get("memory_preamble", "")),
            tool_names=d.get("tool_names"),
            capability_set=d.get("capability_set"),
            model=d.get("model"),
        )


@dataclass
class SubagentResult:
    leaf_id: str
    parent_id: str
    output: str
    trace: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "leaf_id": self.leaf_id,
            "parent_id": self.parent_id,
            "output": self.output,
            "trace": self.trace,
            "error": self.error,
        })


# ---------- Approver ----------


def _deny_approver(*a: Any, **kw: Any) -> bool:
    """Subagent has no TTY; anything not granted by capability is denied."""
    return False


def capability_only_approver(caps: CapabilitySet):
    """Approver that auto-approves on capability match, denies otherwise."""
    return make_capability_aware(_deny_approver, caps)


# ---------- In-process runner (also used by subprocess entry point) ----------


def _run_in_process(
    spec: SubagentSpec,
    *,
    cancel_event=None,
) -> SubagentResult:
    """Run the subagent's executor in the current process.

    This is the path the subprocess entry point invokes after reading
    stdin, and the path tests use to avoid subprocess+LLM overhead.
    `cancel_event` (v1.4): a threading.Event-like; if set, the executor
    returns "[cancelled]" between steps. Only honored on the in-process
    path — the subprocess path doesn't get it (subprocess sub-agents are
    for plan trees, not swarms).
    """
    caps = CapabilitySet.from_dict(spec.capability_set or {})
    tools = default_registry(capabilities=caps, tool_names=spec.tool_names)
    approver = capability_only_approver(caps)

    # v1.4: only forward model= / cancel_event= when set so legacy
    # callers (and test stubs of executor.execute that don't accept the
    # kwargs) keep working.
    exec_kwargs: dict = {}
    if spec.model:
        exec_kwargs["model"] = spec.model
    if cancel_event is not None:
        exec_kwargs["cancel_event"] = cancel_event
    try:
        output, trace = executor.execute(
            original_request=spec.request,
            chosen_label=spec.label,
            chosen_action=spec.action,
            tools=tools,
            approver=approver,
            on_step=None,
            skill_body=spec.skill_body,
            memory_preamble=spec.memory_preamble,
            **exec_kwargs,
        )
        result = SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output=output, trace=trace, error=None,
        )
    except Exception as e:
        result = SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output="", trace=[], error=f"{type(e).__name__}: {e}",
        )

    # Log the subagent run linked to the parent record by `parent_id`.
    try:
        logger.write({
            "ts": logger.now_iso(),
            "type": "subagent",
            "parent_id": spec.parent_id,
            "leaf_id": spec.leaf_id,
            "description": spec.description,
            "request": spec.request,
            "label": spec.label,
            "action": spec.action,
            "tool_names": spec.tool_names,
            "output": result.output,
            "trace": result.trace,
            "error": result.error,
        })
    except Exception:
        # Logging failure never propagates into the executor loop (P8).
        pass

    return result


# ---------- Subprocess runner (production default) ----------


def _run_subprocess(spec: SubagentSpec) -> SubagentResult:
    """Spawn `python -m janus.subagent` as a child process.

    The child sets `JANUS_IS_SUBAGENT=1` so the recursion guard there
    refuses any further subagent spawning.
    """
    env = dict(os.environ)
    env["JANUS_IS_SUBAGENT"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "janus.subagent"],
            input=spec.to_json(),
            capture_output=True,
            text=True,
            env=env,
            timeout=config.SUBAGENT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output="", trace=[],
            error=f"timeout after {config.SUBAGENT_TIMEOUT}s",
        )
    except Exception as e:
        return SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output="", trace=[],
            error=f"spawn_error: {type(e).__name__}: {e}",
        )

    if proc.returncode != 0:
        return SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output="", trace=[],
            error=f"exit_{proc.returncode}: {proc.stderr.strip()[:500]}",
        )

    blob = proc.stdout.strip()
    if not blob:
        return SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output="", trace=[], error="empty_stdout",
        )
    try:
        # Use the LAST non-empty stdout line as the result envelope so
        # incidental prints from upstream code don't break parsing.
        line = [ln for ln in blob.splitlines() if ln.strip()][-1]
        d = json.loads(line)
    except (json.JSONDecodeError, IndexError):
        return SubagentResult(
            leaf_id=spec.leaf_id, parent_id=spec.parent_id,
            output="", trace=[],
            error=f"unparseable_stdout: {proc.stdout[:500]!r}",
        )

    return SubagentResult(
        leaf_id=d.get("leaf_id", spec.leaf_id),
        parent_id=d.get("parent_id", spec.parent_id),
        output=d.get("output", ""),
        trace=d.get("trace", []) or [],
        error=d.get("error"),
    )


# ---------- Public API ----------


# Tests monkeypatch this attribute to swap the runner.
_RUNNER = _run_subprocess


def run_subagent(spec: SubagentSpec) -> SubagentResult:
    """Run one subagent (subprocess by default)."""
    return _RUNNER(spec)


def run_batch(
    specs: list[SubagentSpec],
    *,
    concurrency: int | None = None,
) -> list[SubagentResult]:
    """Run subagents in parallel up to `concurrency`.

    Returns results in the SAME ORDER as input specs. The orchestrator
    is responsible for ordering specs by leaf id before calling so that
    final output is deterministic regardless of which subagent finishes
    first (the Phase 8 design choice).
    """
    if not specs:
        return []
    n = concurrency if concurrency is not None else config.SUBAGENT_CONCURRENCY
    n = max(1, min(n, len(specs)))
    results: list[SubagentResult | None] = [None] * len(specs)

    def _worker(idx: int) -> None:
        results[idx] = run_subagent(specs[idx])

    if n == 1:
        for i in range(len(specs)):
            _worker(i)
    else:
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_worker, i) for i in range(len(specs))]
            for f in futures:
                f.result()

    return [r for r in results if r is not None]


# ---------- Recursion guard ----------


def is_subagent_env() -> bool:
    """True iff this process was spawned as a subagent (parent set
    `JANUS_IS_SUBAGENT=1`). The orchestrator checks this before spawning;
    nested subagents are forbidden (Phase 8 acceptance criterion #5)."""
    return os.getenv("JANUS_IS_SUBAGENT") == "1"


# ---------- Same-file conflict detection ----------


def write_target_globs(caps_dict: dict | None) -> list[str]:
    """Extract fs.write globs from a serialized capability dict."""
    if not caps_dict:
        return []
    return list(caps_dict.get("fs.write") or [])


def specs_conflict(a_caps: dict | None, b_caps: dict | None) -> bool:
    """True if two leaves' fs.write capabilities are not safe to run in
    parallel. Conservative — when ambiguous, returns True (serialize)."""
    a_globs = write_target_globs(a_caps)
    b_globs = write_target_globs(b_caps)
    if not a_globs or not b_globs:
        return False
    for ag in a_globs:
        for bg in b_globs:
            if _globs_overlap(ag, bg):
                return True
    return False


def _globs_overlap(a: str, b: str) -> bool:
    if a == b:
        return True
    if a in ("**", "**/*", "*"):
        return True
    if b in ("**", "**/*", "*"):
        return True
    if a.startswith("**/") or b.startswith("**/"):
        return True
    a_prefix = _literal_prefix(a)
    b_prefix = _literal_prefix(b)
    if not a_prefix or not b_prefix:
        # Both are wildcard from char zero — assume overlap.
        return True
    return a_prefix.startswith(b_prefix) or b_prefix.startswith(a_prefix)


def _literal_prefix(glob: str) -> str:
    out: list[str] = []
    for ch in glob:
        if ch in "*?[":
            break
        out.append(ch)
    return "".join(out)


# ---------- Subprocess entry point ----------


def _main_subagent_entry() -> int:
    """Invoked when subprocess runs `python -m janus.subagent`.

    Reads spec JSON on stdin → runs in-process → writes result JSON on
    stdout. Returns 0 on success, 1 on hard failure (bad input).
    """
    blob = sys.stdin.read()
    if not blob.strip():
        sys.stderr.write("subagent: empty stdin\n")
        return 1
    try:
        spec = SubagentSpec.from_json(blob)
    except Exception as e:
        sys.stderr.write(f"subagent: bad spec: {type(e).__name__}: {e}\n")
        return 1
    result = _run_in_process(spec)
    sys.stdout.write(result.to_json() + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(_main_subagent_entry())
