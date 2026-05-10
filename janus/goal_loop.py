"""
goal_loop.py — judge model + auto-continue (v1.37.1, Phase 10.1.1).

WHY:
v1.37.0 shipped the /goal state primitive + slash commands. This
module adds the autonomous-worker behavior:
  * After each agent turn, the judge model decides whether the
    standing objective has been achieved.
  * If not, the loop returns the next-step prompt for the surface
    to auto-fire.
  * Cycle detection auto-pauses when the agent is stuck (last 3
    assistant responses are identical).
  * Budget exhaustion auto-pauses when turn_budget is reached.

Surface integration (cli_rich, future telegram/web):
  scope = goal_loop.scope_for(ctx_or_surface)
  decision = goal_loop.after_turn(scope, last_assistant_output)
  if decision.achieved:    # show ✓ goal achieved
  elif decision.paused:    # show ⏸ goal paused, reason
  elif decision.next_prompt:   # auto-fire that prompt next turn

JUDGE MODEL:
config.JUDGE_MODEL (env JANUS_JUDGE_MODEL) — falls back to
config.MODEL when unset. Cheap recommended (haiku / gpt-4o-mini)
because the judge fires once per loop turn — at 500-turn budgets
the cost adds up.

CYCLE DETECTION:
We hash the most recent assistant output (sha1 truncated) and keep
a sliding window of 3 hashes on the GoalState. When all 3 match,
the loop has stalled — auto-pause. The hash bypasses minor stream
artifacts (whitespace, trailing punctuation) so a TRULY-identical
response is what triggers it; partial overlap doesn't.

TEMPERATURE:
Judge runs at temperature=0 for determinism. Same goal + same last
output should yield same achieved/continue verdict.

JSON MODE:
We ask for strict JSON. Parse failure → assume NOT achieved + use
a default next-step ("continue"). The loop should never hard-stop
on a parse error — the budget + cycle detector are the safety
nets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

from . import config, goals


@dataclass
class JudgeResult:
    achieved: bool
    reason: str
    next_step: str   # only meaningful when not achieved


@dataclass
class AutoContinueDecision:
    """Returned by after_turn(). Surfaces drive UX off these flags."""
    next_prompt: Optional[str] = None   # None = stop the auto-loop
    reason: str = ""
    achieved: bool = False
    paused: bool = False
    cycle_detected: bool = False
    budget_exhausted: bool = False
    # v1.37.4: a 50/80/100% turn-budget threshold the goal JUST
    # crossed this turn (None when no threshold crossed). Surfaces
    # render this as a one-line warning. Same threshold won't
    # re-fire — goal.budget_alerts_fired persists across turns.
    budget_alert: Optional[float] = None
    # v1.37.4: cumulative USD spent on this goal so far. Surfaces
    # may surface alongside the alert.
    cost_usd: float = 0.0


# ---------- scope helpers ----------


def scope_for_surface(surface: str, *, extra_scope: Optional[str] = None) -> str:
    """Resolve the storage scope for a surface. Used by cli_rich /
    telegram / web to derive a consistent scope key. Mirrors the
    helper in slash_dispatch._goal_scope so /goal handler and
    auto-continue loop agree on the same file."""
    if extra_scope:
        return str(extra_scope)
    return surface or "default"


# ---------- cycle detection ----------


def _response_hash(text: str) -> str:
    """sha1 of normalized text — strip whitespace runs + lowercase
    so trivial differences don't reset the cycle detector. Empty
    or whitespace-only input returns empty string (no hash) so the
    cycle detector treats blank turns as 'no signal'."""
    if not text or not text.strip():
        return ""
    norm = re.sub(r"\s+", " ", text.strip()).lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _is_cycle(hashes: list[str], new_hash: str) -> bool:
    """True if `new_hash` plus the last 2 entries form three-in-a-row."""
    if not new_hash:
        return False
    if len(hashes) < 2:
        return False
    return hashes[-1] == new_hash and hashes[-2] == new_hash


# ---------- judge ----------


JUDGE_SYSTEM_PROMPT = (
    "You evaluate whether a developer's standing goal has been "
    "achieved based on the agent's most recent turn output. "
    "Respond with a single JSON object only — no prose, no "
    "markdown fences."
)


def _build_judge_prompt(goal_text: str, last_response: str) -> list[dict]:
    """Two-message conversation: judge instructions + the data."""
    # Truncate the last response to a sane size so we don't pay for
    # huge contexts on every judge call. The achievement signal is
    # almost always at the END of the assistant's reply.
    snippet = (last_response or "")[-3000:]
    user = (
        f'GOAL: "{goal_text}"\n\n'
        f'AGENT\'S LAST OUTPUT (truncated to last 3000 chars):\n'
        f'"""\n{snippet}\n"""\n\n'
        'Respond with JSON ONLY in this exact shape:\n'
        '{\n'
        '  "achieved": true | false,\n'
        '  "reason": "<one sentence — why or why not>",\n'
        '  "next_step": "<one sentence — only when not achieved; '
        'the immediate next concrete action the agent should take>"\n'
        '}'
    )
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _parse_judge_response(raw: str) -> Optional[JudgeResult]:
    """Tolerant JSON extraction — strips markdown fences, trims to the
    first/last brace pair. Returns None on unrecoverable parse error
    so callers can fall back to the safe default."""
    if not raw or not raw.strip():
        return None
    # Strip code-fence wrappers: ```json ... ``` or ``` ... ```
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    # Find the outer JSON object
    match = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return JudgeResult(
        achieved=bool(data.get("achieved", False)),
        reason=str(data.get("reason", "") or ""),
        next_step=str(data.get("next_step", "") or ""),
    )


def run_judge(goal_text: str, last_response: str) -> JudgeResult:
    """Call the judge model once and return its verdict.

    Errors fall through to a safe default: NOT achieved, reason
    'judge unavailable', empty next_step. The loop's budget + cycle
    detector are the safety nets — we never hard-stop on a judge
    error, only soft-stop via budget/cycle.
    """
    if not goal_text or not goal_text.strip():
        return JudgeResult(achieved=False, reason="empty goal", next_step="")
    try:
        from . import llm
        msg = llm.chat(
            messages=_build_judge_prompt(goal_text, last_response),
            tools=None,
            json_mode=True,
            temperature=0.0,
            model=config.model_for_purpose("judge"),
        )
    except Exception as e:
        return JudgeResult(
            achieved=False,
            reason=f"judge unavailable: {type(e).__name__}",
            next_step="",
        )

    raw = ""
    if isinstance(msg, dict):
        # Anthropic / OpenAI both surface text under 'content'
        c = msg.get("content")
        if isinstance(c, str):
            raw = c
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and block.get("type") == "text":
                    raw += str(block.get("text", ""))
                elif isinstance(block, str):
                    raw += block

    parsed = _parse_judge_response(raw)
    if parsed is None:
        return JudgeResult(
            achieved=False,
            reason="judge response unparseable",
            next_step="",
        )
    return parsed


# ---------- the orchestrator ----------


# Cap recent_response_hashes to this many entries.
_HASH_WINDOW = 3

# v1.37.4: turn-budget alert thresholds (fraction of turn_budget).
# Crossing each threshold fires once per goal — surfaces render a
# one-line warning. 1.0 fires immediately before the budget-
# exhausted pause, so the user knows why it just stopped.
BUDGET_ALERT_THRESHOLDS: tuple[float, ...] = (0.5, 0.8, 1.0)


def _check_budget_alert(g: "goals.GoalState") -> Optional[float]:
    """Return the highest threshold the goal JUST crossed (i.e.
    crossed AND not yet alerted). Mutates g.budget_alerts_fired.
    Returns None when no new threshold was crossed."""
    ratio = g.progress_ratio()
    fired_now: Optional[float] = None
    for threshold in BUDGET_ALERT_THRESHOLDS:
        if ratio >= threshold and threshold not in g.budget_alerts_fired:
            g.budget_alerts_fired.append(threshold)
            fired_now = threshold  # last-write-wins → highest threshold
    return fired_now


def _accumulate_cost(g: "goals.GoalState") -> None:
    """Add the latest turn's spend to the goal's running total.
    Best-effort: if cost.turn_stats() is unavailable, leaves the
    field unchanged."""
    try:
        from . import cost
        usd = float(cost.turn_stats().usd or 0.0)
        if usd > 0:
            g.cost_usd += usd
    except Exception:
        pass


def after_turn(scope: str, last_response: str) -> AutoContinueDecision:
    """Run the post-turn pipeline for the goal at `scope`.

    Returns an AutoContinueDecision describing what should happen
    next: keep going (next_prompt set), achievement/pause (no
    next_prompt). The surface is responsible for the UX.
    """
    g = goals.load(scope)
    if g is None or g.status != "active":
        # No goal, or goal isn't active → no auto-continue.
        return AutoContinueDecision(reason="no active goal")

    # Bump the turn counter FIRST so an exhausted budget reads
    # consistently from /goal status afterward.
    g.turns_used += 1

    # v1.37.4: cost tracking — capture this turn's LLM spend
    # BEFORE any further processing so it sticks to disk.
    _accumulate_cost(g)

    # v1.37.4: 50/80/100% turn-budget alerts (one-shot per
    # threshold). Computed here so 'budget exhausted' (1.0) fires
    # alongside the budget-exhausted pause below.
    alert = _check_budget_alert(g)

    # Budget exhausted? Pause and stop.
    if g.budget_exhausted():
        g.status = "paused"
        goals.save(scope, g)
        return AutoContinueDecision(
            paused=True,
            budget_exhausted=True,
            reason=f"turn budget exhausted ({g.turns_used}/{g.turn_budget})",
            budget_alert=alert,
            cost_usd=g.cost_usd,
        )

    # Cycle detection BEFORE judging — saves a judge call when stuck.
    new_hash = _response_hash(last_response)
    if _is_cycle(g.recent_response_hashes, new_hash):
        g.status = "paused"
        # Append the new hash so next /goal status shows the streak.
        g.recent_response_hashes = (
            g.recent_response_hashes + [new_hash]
        )[-_HASH_WINDOW:]
        goals.save(scope, g)
        return AutoContinueDecision(
            paused=True,
            cycle_detected=True,
            reason="last 3 assistant outputs were identical — agent stalled",
            budget_alert=alert,
            cost_usd=g.cost_usd,
        )

    # Update the sliding window before judging.
    g.recent_response_hashes = (
        g.recent_response_hashes + [new_hash]
    )[-_HASH_WINDOW:]
    goals.save(scope, g)

    # Run the judge. Errors fall back to "not achieved + empty
    # next_step", which we recover with a default continuation.
    verdict = run_judge(g.text, last_response)
    if verdict.achieved:
        g = goals.load(scope) or g
        g.status = "done"
        goals.save(scope, g)
        return AutoContinueDecision(
            achieved=True,
            reason=verdict.reason or "judge says goal achieved",
            budget_alert=alert,
            cost_usd=g.cost_usd,
        )

    # Build the next prompt. If the judge gave us a hint, use it
    # verbatim — that's the literal continuation prompt for the
    # next turn. If empty, fall back to a generic "continue".
    next_prompt = (verdict.next_step or "").strip()
    if not next_prompt:
        next_prompt = (
            f"Continue working toward the goal: {g.text}\n"
            f"What's the next concrete step?"
        )

    return AutoContinueDecision(
        next_prompt=next_prompt,
        reason=verdict.reason or "judge says continue",
        budget_alert=alert,
        cost_usd=g.cost_usd,
    )


def is_active(scope: str) -> bool:
    """True iff a goal exists, is active, and has remaining budget."""
    g = goals.load(scope)
    return bool(g and g.status == "active" and not g.budget_exhausted())
