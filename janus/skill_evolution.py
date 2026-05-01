"""
skill_evolution.py — propose body/capability revisions to skills based on use.

WHY:
Skills as static markdown files are weaker than skills that sharpen with use.
Hermes' moat is auto-evolving skills; ours has to evolve too — but with
manual approval at every step (P4) and with capabilities locked behind an
explicit per-skill opt-in (P2).

THE LOOP:
1. cli/cli_rich call skills.record_run() after each skill use, passing
   success=True/False/None per the heuristic+explicit policy.
2. After every N runs (default 5, env JANUS_SKILL_REVIEW_EVERY), the CLI
   prints a hint: "skill X has N runs; consider /skill review X".
3. /skill review <name> calls propose_revision() — one LLM call returning a
   JSON revision proposal. The user reads the diff and approves or rejects.
4. apply_revision() persists the change atomically (skills.save uses
   write-tmp + os.replace).

EXPLICIT NON-GOALS:
- Auto-applying revisions. P4 forbids it.
- Modifying capabilities by default. The skill must opt in via
  `evolve-capabilities: true` in its frontmatter. Capability tokens are the
  security primitive; broadening them via an LLM diff inverts the trust
  direction even with y/N approval, so the second gate is required.
"""

from __future__ import annotations
import json
from typing import Any

from . import config, llm, logger, skills as skills_mod
from .tools.capabilities import CapabilitySet


# ---------- Trigger predicate ----------


def should_propose(skill: skills_mod.Skill, threshold: int | None = None) -> bool:
    """True iff the skill has accumulated enough runs to merit a review.

    Triggers exactly at runs = N, 2N, 3N, ... where N is the threshold.
    Returning True at every multiple is intentional — the hint is a
    suggestion, not an action; the user gates whether to actually run
    `/skill review`.
    """
    n = threshold if threshold is not None else config.SKILL_REVIEW_EVERY
    if n <= 0:
        return False
    return skill.runs > 0 and (skill.runs % n) == 0


# ---------- Log lookup ----------


def recent_skill_runs(skill_name: str, k: int = 20) -> list[dict]:
    """Return the last k log records that used this skill.

    Used both by propose_revision() and by `python -m janus --eval --skill X`
    to filter replay to one skill's history.
    """
    matched: list[dict] = []
    for rec in logger.read_all():
        if rec.get("skill") == skill_name:
            matched.append(rec)
    return matched[-k:]


# ---------- LLM proposal ----------


_REVISION_TMPL = """You refine a Janus skill's prompt body based on recent
runs and explicit user feedback.

You will receive (as JSON):
- the skill name, description, and current body
- run statistics: runs / success / fail
- recent runs: request, output excerpt, and feedback (good / bad / null)
{capabilities_clause}

Decide whether the skill body would clearly improve. Examples of an
improvement worth proposing:
- Add a step the agent forgot in failed runs.
- Remove or simplify a step that produced wasted work in successful runs.
- Tighten ambiguous instructions that the runs show were misread.

Be CONSERVATIVE. The right answer is usually no change. Do NOT revise just
because the body could be phrased better; revise only when the recent runs
themselves are evidence of a problem the new body fixes.

Return STRICT JSON.

When proposing no change:
  {{"changed": false, "rationale": "<one sentence>"}}

When proposing a change:
  {{"changed": true,
    "rationale": "<one sentence citing the runs that motivated this>",
    "body": "<full new markdown body, complete replacement>"{capabilities_field}}}

The new body MUST be a complete replacement, not a diff. The rationale MUST
cite specific recent runs.

No prose, no markdown fences, no commentary."""


def _build_system_prompt(evolve_caps: bool) -> str:
    if evolve_caps:
        cap_clause = "- the current capabilities (you may also propose new ones)"
        cap_field = ', "capabilities": {"shell.exec": ["..."], "fs.read": ["..."]}'
    else:
        cap_clause = ""
        cap_field = ""
    return _REVISION_TMPL.format(
        capabilities_clause=cap_clause,
        capabilities_field=cap_field,
    )


def propose_revision(
    skill: skills_mod.Skill,
    *,
    log_records: list[dict] | None = None,
    k: int = 20,
) -> dict:
    """Ask the LLM for a revision proposal.

    Returns a dict matching the JSON shape in _REVISION_TMPL. On any LLM /
    parsing failure we return {"changed": False, "rationale": "..."} —
    never raise. Per P8, errors are observations.

    `log_records` overrides the live log (test seam). Otherwise reads up to
    `k` most-recent records that referenced this skill.
    """
    records = (
        log_records
        if log_records is not None
        else recent_skill_runs(skill.name, k)
    )
    evolve_caps = skill.evolve_capabilities_enabled()

    excerpt: list[dict] = []
    for r in records[-10:]:
        excerpt.append({
            "request": (r.get("request") or "")[:300],
            "output_head": (r.get("output") or "")[:300],
            "feedback": r.get("feedback"),
        })

    payload: dict[str, Any] = {
        "name": skill.name,
        "description": skill.description,
        "current_body": skill.body,
        "stats": {
            "runs": skill.runs,
            "success": skill.success,
            "fail": skill.fail,
        },
        "recent_runs": excerpt,
    }
    if evolve_caps:
        payload["current_capabilities"] = _caps_to_serializable(skill.capabilities)

    sys_prompt = _build_system_prompt(evolve_caps)

    try:
        msg = llm.chat(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            json_mode=True,
            temperature=0.2,
        )
    except Exception as e:
        return {"changed": False, "rationale": f"(LLM call failed: {type(e).__name__})"}

    try:
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception:
        return {"changed": False, "rationale": "(LLM returned unparseable JSON)"}

    if not isinstance(data, dict):
        return {"changed": False, "rationale": "(LLM returned non-object)"}

    changed = bool(data.get("changed"))
    out: dict[str, Any] = {
        "changed": changed,
        "rationale": str(data.get("rationale") or "")[:500],
    }
    if not changed:
        return out

    body = str(data.get("body") or "").strip()
    if not body:
        return {
            "changed": False,
            "rationale": "(LLM declared change but provided empty body)",
        }
    out["body"] = body

    if evolve_caps and isinstance(data.get("capabilities"), dict):
        # Sanity: every key looks like "tool.verb"; values are list of strings.
        caps = data["capabilities"]
        clean: dict[str, list[str]] = {}
        for k_, v_ in caps.items():
            if not isinstance(k_, str) or "." not in k_:
                continue
            if not isinstance(v_, list):
                continue
            clean[k_] = [str(g) for g in v_ if isinstance(g, str)]
        if clean:
            out["capabilities"] = clean

    return out


# ---------- Apply ----------


def apply_revision(
    skill: skills_mod.Skill,
    revision: dict,
) -> skills_mod.Skill:
    """Persist an approved revision atomically.

    The caller (slash command handler) is responsible for showing the diff
    and getting explicit y/N. This function applies blindly — it is the
    persistence side, not the gate.

    Capabilities are applied ONLY if the skill has
    `evolve-capabilities: true` in its frontmatter, regardless of what the
    revision contains.
    """
    if not revision.get("changed"):
        return skill

    body = str(revision.get("body") or "").strip()
    if body:
        skill.body = body

    if skill.evolve_capabilities_enabled():
        caps_dict = revision.get("capabilities")
        if isinstance(caps_dict, dict) and caps_dict:
            skill.capabilities = CapabilitySet.from_dict(caps_dict)

    skills_mod.save(skill)
    return skill


# ---------- Render ----------


def render_revision(skill: skills_mod.Skill, revision: dict) -> str:
    """Pretty-print a revision proposal for terminal review."""
    if not revision.get("changed"):
        rationale = revision.get("rationale", "")
        return f"no change proposed.\n  rationale: {rationale}"

    lines: list[str] = []
    lines.append(f"rationale: {revision.get('rationale', '')}")
    lines.append("")
    lines.append("--- current body ---")
    lines.append(skill.body)
    lines.append("")
    lines.append("--- proposed body ---")
    lines.append(str(revision.get("body", "")))

    if "capabilities" in revision and skill.evolve_capabilities_enabled():
        lines.append("")
        lines.append("--- current capabilities ---")
        lines.append(json.dumps(_caps_to_serializable(skill.capabilities), indent=2))
        lines.append("")
        lines.append("--- proposed capabilities ---")
        lines.append(json.dumps(revision["capabilities"], indent=2))
    elif "capabilities" in revision:
        lines.append("")
        lines.append(
            "(capabilities in proposal IGNORED — skill does not opt in via "
            "`evolve-capabilities: true`)"
        )

    return "\n".join(lines)


# ---------- Helpers ----------


def _caps_to_serializable(caps: CapabilitySet) -> dict[str, list[str]]:
    return {f"{c.tool}.{c.verb}": list(c.globs) for c in caps.caps}


# ---------- Heuristic for cli/cli_rich ----------


def infer_success(output: str, trace: list | None) -> bool | None:
    """Heuristic success signal when the user did not give explicit +/-.

    Returns True (looks good), False (looks bad), or None (no signal).

    Per P8, this never raises. Callers wrap in try/except anyway.
    """
    out = (output or "").strip()
    if not out:
        return False

    out_lower = out.lower()
    bad_markers = ("error:", "traceback", "failed:", "exception:")
    if any(m in out_lower for m in bad_markers):
        return False

    # plan-tree style: list of {leaf, trace, error}
    for step in trace or []:
        if isinstance(step, dict) and step.get("error"):
            return False

    return True


def resolve_success(
    output: str,
    trace: list | None,
    explicit_feedback: str | None,
) -> bool | None:
    """Combine explicit feedback (+/-) with the heuristic.

    Per the user's Phase 7 decision: heuristic by default, explicit overrides.
        explicit_feedback "good"   → True
        explicit_feedback "bad"    → False
        otherwise                   → infer_success()
    """
    if explicit_feedback == "good":
        return True
    if explicit_feedback == "bad":
        return False
    return infer_success(output, trace)
