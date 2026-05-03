"""
swarms/budget.py — runtime budget enforcement for a single swarm run.

Caps enforced (each is a hard kill condition; first to trip wins):
  - max_usd                    swarm-wide $ spent
  - max_wallclock_s            seconds since start
  - max_subagents              number dispatched (across all phases)
  - max_total_tool_calls       sum of all sub-agents' tool calls

The per-spec budget block is the LOWER BOUND of the kill condition; the
config.SWARM_MAX_* hard ceilings (validated in spec.py) are the upper
bound. A spec asking for more than the system permits was rejected at
parse time; here we just trust the validated values.

Cost feed: swarm-wide $ comes from cost.per_swarm_summary(swarm_run_id),
which is populated by cost.record() on the LLM call thread (thread-local
attribution path added in v1.4 phase 4). Accurate even with parallel
sub-agents.

Kill check is fired AT PHASE BOUNDARIES + AFTER EACH SUB-AGENT RETURNS.
We do NOT poll mid-call (that would require plumbing through executor.chat
and llm.chat — violates P6 thin call path). MAX_STEPS + LLM_TIMEOUT bound
the per-sub-agent damage between checks.
"""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field

from .. import cost
from . import spec as spec_mod


@dataclass
class BudgetState:
    """Counters maintained by the swarm runner. Thread-safe via _lock."""
    n_subagents_dispatched: int = 0
    n_total_tool_calls: int = 0
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class BudgetVerdict:
    """Result of a budget check. `allowed=False` → coordinator must stop."""
    allowed: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> "BudgetVerdict":
        return cls(allowed=True, reason="")

    @classmethod
    def deny(cls, reason: str) -> "BudgetVerdict":
        return cls(allowed=False, reason=reason)


class SwarmBudget:
    """Per-run budget tracker + kill-switch.

    Construct once per swarm run, before phase 0 dispatch. Call
    `register_dispatch()` once per sub-agent BEFORE dispatching.
    Call `register_complete(tool_call_count)` when each returns.
    Call `check(swarm_run_id)` between sub-agents and between phases.
    """

    def __init__(self, budget: spec_mod.Budget):
        self.budget = budget
        self.state = BudgetState()

    # ---------- Counters ----------

    def register_dispatch(self) -> None:
        with self.state._lock:
            self.state.n_subagents_dispatched += 1

    def register_complete(self, tool_call_count: int = 0) -> None:
        with self.state._lock:
            self.state.n_total_tool_calls += int(tool_call_count or 0)

    # ---------- Snapshots ----------

    def usd_spent(self, swarm_run_id: str) -> float:
        """Read swarm-wide $ from the run-local cost ledger."""
        st = cost.per_swarm_summary(swarm_run_id)
        return float(st.usd or 0.0)

    def wallclock_elapsed(self) -> float:
        return time.time() - self.state.started_at

    # ---------- Kill checks ----------

    def check(self, swarm_run_id: str) -> BudgetVerdict:
        """Evaluate every cap. Returns first violation or ok()."""
        # Wall-clock first — cheapest check.
        if self.wallclock_elapsed() > self.budget.max_wallclock_s:
            return BudgetVerdict.deny(
                f"wallclock_exceeded: "
                f"{self.wallclock_elapsed():.1f}s > {self.budget.max_wallclock_s}s"
            )
        # Sub-agent count.
        with self.state._lock:
            n_sub = self.state.n_subagents_dispatched
            n_tools = self.state.n_total_tool_calls
        if n_sub > self.budget.max_subagents:
            return BudgetVerdict.deny(
                f"max_subagents_exceeded: {n_sub} > {self.budget.max_subagents}"
            )
        if n_tools > self.budget.max_total_tool_calls:
            return BudgetVerdict.deny(
                f"max_total_tool_calls_exceeded: "
                f"{n_tools} > {self.budget.max_total_tool_calls}"
            )
        # USD last (requires a file read).
        spent = self.usd_spent(swarm_run_id)
        if spent > self.budget.max_usd:
            return BudgetVerdict.deny(
                f"max_usd_exceeded: ${spent:.4f} > ${self.budget.max_usd:.4f}"
            )
        return BudgetVerdict.ok()

    def can_dispatch_n_more(self, n: int) -> BudgetVerdict:
        """Pre-flight: would dispatching `n` more sub-agents exceed the cap?"""
        with self.state._lock:
            already = self.state.n_subagents_dispatched
        if already + n > self.budget.max_subagents:
            return BudgetVerdict.deny(
                f"max_subagents_would_exceed: "
                f"{already}+{n} > {self.budget.max_subagents}"
            )
        return BudgetVerdict.ok()

    # ---------- Display ----------

    def snapshot(self, swarm_run_id: str) -> dict:
        with self.state._lock:
            n_sub = self.state.n_subagents_dispatched
            n_tools = self.state.n_total_tool_calls
        return {
            "usd_spent": round(self.usd_spent(swarm_run_id), 6),
            "usd_max": self.budget.max_usd,
            "wallclock_s": round(self.wallclock_elapsed(), 1),
            "wallclock_max_s": self.budget.max_wallclock_s,
            "n_subagents": n_sub,
            "n_subagents_max": self.budget.max_subagents,
            "n_tool_calls": n_tools,
            "n_tool_calls_max": self.budget.max_total_tool_calls,
        }
