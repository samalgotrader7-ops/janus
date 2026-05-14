"""
skill_gepa.py — offline evolutionary engine for skill body refinement (v1.44.0).

WHY:
Janus already ships skill_evolution.py (v1.20.x), but that's a SINGLE-shot
LLM-revision-with-y/N — one LLM call returns "change yes/no + new body."
Useful, but it's not search: it commits to whichever direction the model
proposes first, with no comparison against alternatives.

GEPA = Genetic-Evolutionary-Population-Approach for skills. It:

  1. Starts from the current body as baseline.
  2. Spawns N variants per generation via 4 mutation operators
     (rewrite, simplify, specialize, crossover).
  3. Scores each variant offline by LLM-judging it against a replay
     corpus pulled from log.jsonl (records that actually invoked this
     skill historically).
  4. Selects the top-K survivors by fitness (ties broken by body length —
     shorter = better, anti-bloat).
  5. Repeats for G generations.
  6. Emits a JSON artifact at ``~/.janus/skills/_gepa/<skill>/<run>.json``
     with full provenance + a recommendation.

P4 INVARIANT: NEVER auto-applies. The artifact is a proposal; the caller
(CLI / MCP) shows the diff and asks y/N. apply_best() is exposed as the
persistence side — same trust ladder as skill_evolution.apply_revision().

COST GUARDS:
  * Hard cap on total LLM calls per run (JANUS_GEPA_MAX_LLM_CALLS).
  * Body-hash-keyed cache so equivalent variants aren't re-judged.
  * Records capped at JANUS_GEPA_RECORDS_PER_RUN (default 10).
  * Defaults: pop=6, gen=3, records=10 → ~200 calls/run. Acceptable on
    Ollama Turbo cloud (Sam's setup); cap raisable for paid endpoints.

TEST SEAM:
  Every LLM-touching helper (_judge, _mutate_*) takes its callable from
  module-level globals. Tests monkeypatch these directly — no LLM dep.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import random
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, llm, logger, skills as skills_mod


# ============================================================
# Data classes
# ============================================================


@dataclass
class Variant:
    id: str
    body: str
    fitness: float  # 0..100 mean across records
    operator: str   # "baseline" | "rewrite" | "simplify" | "specialize" | "crossover"
    parents: list[str] = field(default_factory=list)
    per_record: list[dict] = field(default_factory=list)  # [{"record_idx": int, "score": float, "rationale": str}]
    generation: int = 0

    def body_hash(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:12]


@dataclass
class Generation:
    index: int
    variants: list[Variant]


@dataclass
class GepaResult:
    skill_name: str
    run_id: str
    started_at: str
    ended_at: str
    config: dict
    baseline: Variant
    generations: list[Generation]
    best: Variant
    improvement: float
    recommendation: str  # "apply" | "no_change" | "no_signal"
    artifact_path: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "config": self.config,
            "baseline": asdict(self.baseline),
            "generations": [
                {"index": g.index, "variants": [asdict(v) for v in g.variants]}
                for g in self.generations
            ],
            "best": asdict(self.best),
            "improvement": self.improvement,
            "recommendation": self.recommendation,
            "artifact_path": self.artifact_path,
            "notes": self.notes,
        }


# ============================================================
# Module-level LLM seams (tests monkeypatch these)
# ============================================================


_JUDGE_SYS = """You score a candidate Janus skill body against a historical
work record. The score reflects whether an agent guided by this body would
have produced an output equivalent-or-better than the historical original.

Scoring rubric:
  100 — clearly would handle this case correctly given this body
   75 — mostly correct; minor mismatches
   50 — partial fit; some essential steps missing or wrong
   25 — fundamentally weak fit; would likely fail this case
    0 — irrelevant or actively harmful body for this request

Return STRICT JSON with this shape only:
  {"score": <int 0-100>, "rationale": "<one sentence>"}

No prose, no fences, no extra fields."""


def _judge(body: str, record: dict) -> tuple[float, str]:
    """LLM-judge one (variant_body × historical_record) pair.

    Returns (score, rationale). On any error returns (0.0, "(judge failed)").
    Module-level so tests can monkeypatch.
    """
    payload = {
        "candidate_body": body[:6000],
        "request": (record.get("request") or "")[:600],
        "original_output_head": (record.get("output") or "")[:600],
        "original_feedback": record.get("feedback"),
    }
    try:
        msg = llm.chat(
            messages=[
                {"role": "system", "content": _JUDGE_SYS},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            json_mode=True,
            temperature=0.0,
        )
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception as e:
        return 0.0, f"(judge LLM failed: {type(e).__name__})"

    if not isinstance(data, dict):
        return 0.0, "(judge returned non-object)"
    try:
        score = float(data.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(100.0, score))
    rationale = str(data.get("rationale") or "")[:300]
    return score, rationale


_REWRITE_SYS = """You rewrite a Janus skill body to handle the failure cases
shown below better. Keep what already works; tighten or replace what doesn't.

Return STRICT JSON: {"body": "<full new markdown body>"}. The body must be
a complete replacement, not a diff. Do not include frontmatter."""


_SIMPLIFY_SYS = """You shorten a Janus skill body without losing essential
steps. Remove redundancy, vague hedges, and unnecessary preamble. Keep all
named actions and structural elements.

Return STRICT JSON: {"body": "<full new markdown body>"}."""


_SPECIALIZE_SYS = """You specialize a Janus skill body for the example cases
shown below. Add concrete guidance for the patterns in these examples —
specific tool calls, specific decision criteria, specific output shapes.

Return STRICT JSON: {"body": "<full new markdown body>"}."""


_CROSSOVER_SYS = """You combine two Janus skill bodies into one. Keep the
strongest sections of each. The result should read as a single coherent
procedure, not two appended halves.

Return STRICT JSON: {"body": "<full new markdown body>"}."""


def _llm_body(sys_prompt: str, user_payload: dict) -> str:
    """Shared shim for all _mutate_* operators. On any error returns ""."""
    try:
        msg = llm.chat(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            json_mode=True,
            temperature=0.6,
        )
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("body") or "").strip()


def _mutate_rewrite(parent: Variant, failure_records: list[dict]) -> str:
    excerpt = [
        {
            "request": (r.get("request") or "")[:300],
            "output": (r.get("output") or "")[:300],
            "feedback": r.get("feedback"),
        }
        for r in failure_records[:5]
    ]
    return _llm_body(_REWRITE_SYS, {"current_body": parent.body, "failures": excerpt})


def _mutate_simplify(parent: Variant) -> str:
    return _llm_body(_SIMPLIFY_SYS, {"current_body": parent.body})


def _mutate_specialize(parent: Variant, example_records: list[dict]) -> str:
    excerpt = [
        {
            "request": (r.get("request") or "")[:300],
            "output_head": (r.get("output") or "")[:300],
        }
        for r in example_records[:5]
    ]
    return _llm_body(_SPECIALIZE_SYS, {"current_body": parent.body, "examples": excerpt})


def _mutate_crossover(parent_a: Variant, parent_b: Variant) -> str:
    return _llm_body(
        _CROSSOVER_SYS,
        {"body_a": parent_a.body, "body_b": parent_b.body},
    )


# ============================================================
# Replay corpus
# ============================================================


def collect_records(skill_name: str, k: int) -> list[dict]:
    """Pull the last k log records that mention this skill.

    Defensive: log records may not have a "skill" field; we accept any
    record whose ``skill == skill_name``. Returns a list possibly shorter
    than k (or empty)."""
    matched: list[dict] = []
    try:
        for rec in logger.read_all():
            if rec.get("skill") == skill_name:
                matched.append(rec)
    except Exception:
        return []
    return matched[-k:]


# ============================================================
# Selection
# ============================================================


def _select_topk(variants: list[Variant], k: int) -> list[Variant]:
    """Sort by (-fitness, len(body)) — best fitness first, shorter ties."""
    return sorted(variants, key=lambda v: (-v.fitness, len(v.body)))[:k]


# ============================================================
# Orchestration
# ============================================================


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "skill"


def _artifact_path(skill_name: str, run_id: str) -> Path:
    base = config.SKILLS_DIR / "_gepa" / _slug(skill_name)
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{run_id}.json"


def _score_variant(
    variant: Variant,
    records: list[dict],
    judge: Callable[[str, dict], tuple[float, str]],
    cache: dict[str, list[dict]],
    call_budget: list[int],  # mutable singleton list; [0] = remaining
) -> None:
    """Populate variant.fitness + per_record. Mutates variant in place.

    Uses cache keyed on (body_hash, record_idx) so duplicate variant bodies
    don't re-judge. call_budget[0] is decremented per fresh LLM call.
    """
    body_h = variant.body_hash()
    cached_pr = cache.get(body_h)
    if cached_pr is not None:
        variant.per_record = list(cached_pr)
        scores = [p["score"] for p in variant.per_record]
        variant.fitness = sum(scores) / max(1, len(scores))
        return

    per_record: list[dict] = []
    for idx, r in enumerate(records):
        if call_budget[0] <= 0:
            # Budget exhausted; treat untested records as 0 (defensive,
            # signal that this variant got cut short). The orchestrator
            # also stops handing out work but we guard here too.
            per_record.append({"record_idx": idx, "score": 0.0, "rationale": "(budget exhausted)"})
            continue
        score, rationale = judge(variant.body, r)
        call_budget[0] -= 1
        per_record.append({"record_idx": idx, "score": score, "rationale": rationale})

    variant.per_record = per_record
    cache[body_h] = per_record
    scores = [p["score"] for p in per_record]
    variant.fitness = sum(scores) / max(1, len(scores))


def evolve(
    skill_name: str,
    *,
    generations: int | None = None,
    population: int | None = None,
    records: list[dict] | None = None,
    record_count: int | None = None,
    max_llm_calls: int | None = None,
    seed: int | None = None,
    on_progress: Callable[[dict], None] | None = None,
    persist_artifact: bool = True,
    # Test seams — tests inject deterministic fakes.
    _judge_fn: Callable[[str, dict], tuple[float, str]] | None = None,
    _rewrite_fn: Callable[[Variant, list[dict]], str] | None = None,
    _simplify_fn: Callable[[Variant], str] | None = None,
    _specialize_fn: Callable[[Variant, list[dict]], str] | None = None,
    _crossover_fn: Callable[[Variant, Variant], str] | None = None,
) -> GepaResult:
    """Run a GEPA pass on ``skill_name``. Returns the GepaResult.

    Never raises into the caller. On unrecoverable error, returns a result
    with ``recommendation="no_signal"`` and a note explaining what went
    wrong. The caller decides what to surface.
    """
    generations = generations if generations is not None else config.GEPA_GENERATIONS
    population = population if population is not None else config.GEPA_POPULATION
    record_count = (
        record_count if record_count is not None else config.GEPA_RECORDS_PER_RUN
    )
    max_llm_calls = (
        max_llm_calls if max_llm_calls is not None else config.GEPA_MAX_LLM_CALLS
    )

    judge = _judge_fn or _judge
    op_rewrite = _rewrite_fn or _mutate_rewrite
    op_simplify = _simplify_fn or _mutate_simplify
    op_specialize = _specialize_fn or _mutate_specialize
    op_crossover = _crossover_fn or _mutate_crossover

    rng = random.Random(seed)
    run_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()

    skill = skills_mod.load(skill_name)
    if skill is None:
        return GepaResult(
            skill_name=skill_name,
            run_id=run_id,
            started_at=started_at,
            ended_at=_now_iso(),
            config={"population": population, "generations": generations},
            baseline=Variant(id="baseline", body="", fitness=0.0, operator="baseline"),
            generations=[],
            best=Variant(id="baseline", body="", fitness=0.0, operator="baseline"),
            improvement=0.0,
            recommendation="no_signal",
            notes=[f"skill '{skill_name}' not found"],
        )

    if records is None:
        records = collect_records(skill_name, record_count)
    records = records[:record_count]

    if not records:
        return GepaResult(
            skill_name=skill_name,
            run_id=run_id,
            started_at=started_at,
            ended_at=_now_iso(),
            config={"population": population, "generations": generations},
            baseline=Variant(
                id="baseline", body=skill.body, fitness=0.0, operator="baseline",
            ),
            generations=[],
            best=Variant(
                id="baseline", body=skill.body, fitness=0.0, operator="baseline",
            ),
            improvement=0.0,
            recommendation="no_signal",
            notes=[
                f"no replay records found for '{skill_name}' — "
                "skill must be used at least once before GEPA can score variants"
            ],
        )

    # Budget singleton — mutated in place by _score_variant.
    budget = [int(max_llm_calls)]
    cache: dict[str, list[dict]] = {}

    # ---- Baseline ----
    baseline = Variant(
        id="baseline",
        body=skill.body,
        fitness=0.0,
        operator="baseline",
        generation=-1,
    )
    _score_variant(baseline, records, judge, cache, budget)
    if on_progress:
        on_progress({"phase": "baseline", "fitness": baseline.fitness})

    gens: list[Generation] = []
    survivors = [baseline]

    keep_k = max(2, population // 3)

    for g in range(generations):
        new_variants: list[Variant] = []

        # Always carry the best survivor as the "elite" of this generation.
        elite = survivors[0]
        new_variants.append(elite)

        ops_cycle = ["rewrite", "simplify", "specialize", "crossover"]
        idx = 0
        while len(new_variants) < population:
            if budget[0] <= 0:
                break
            op = ops_cycle[idx % len(ops_cycle)]
            idx += 1
            parent = rng.choice(survivors)

            # For "failures" vs "examples" — rank records by parent's
            # per_record scores; mutate against low-scorers for rewrite,
            # high-scorers for specialize.
            ranked = sorted(
                enumerate(parent.per_record),
                key=lambda kv: kv[1]["score"],
            )
            failure_records = [records[i] for i, _ in ranked[:3]]
            example_records = [records[i] for i, _ in ranked[-3:]]

            if op == "rewrite":
                body = op_rewrite(parent, failure_records)
                parents = [parent.id]
            elif op == "simplify":
                body = op_simplify(parent)
                parents = [parent.id]
            elif op == "specialize":
                body = op_specialize(parent, example_records)
                parents = [parent.id]
            else:  # crossover
                other = rng.choice(survivors) if len(survivors) > 1 else parent
                body = op_crossover(parent, other)
                parents = [parent.id, other.id]

            # Mutation call costs 1 LLM call regardless of outcome.
            budget[0] -= 1

            body = (body or "").strip()
            if not body or body == parent.body:
                # Mutation produced nothing useful — skip without scoring.
                continue

            variant = Variant(
                id=f"g{g}_{op}_{len(new_variants)}",
                body=body,
                fitness=0.0,
                operator=op,
                parents=parents,
                generation=g,
            )
            _score_variant(variant, records, judge, cache, budget)
            new_variants.append(variant)

            if on_progress:
                on_progress({
                    "phase": "variant",
                    "gen": g,
                    "op": op,
                    "fitness": variant.fitness,
                    "budget_remaining": budget[0],
                })

        gens.append(Generation(index=g, variants=new_variants))
        # Pool elite + new for selection.
        pool = list({v.id: v for v in survivors + new_variants}.values())
        survivors = _select_topk(pool, keep_k)
        if on_progress:
            on_progress({
                "phase": "selection",
                "gen": g,
                "survivors": [(v.id, v.fitness) for v in survivors],
            })
        if budget[0] <= 0:
            break

    best = survivors[0]
    improvement = best.fitness - baseline.fitness

    # Recommendation gate.
    if best.id == "baseline":
        recommendation = "no_change"
    elif improvement >= config.GEPA_PROMOTE_MARGIN:
        recommendation = "apply"
    else:
        recommendation = "no_change"

    result = GepaResult(
        skill_name=skill_name,
        run_id=run_id,
        started_at=started_at,
        ended_at=_now_iso(),
        config={
            "population": population,
            "generations": generations,
            "record_count": len(records),
            "max_llm_calls": max_llm_calls,
            "budget_remaining": budget[0],
            "seed": seed,
        },
        baseline=baseline,
        generations=gens,
        best=best,
        improvement=improvement,
        recommendation=recommendation,
    )

    if persist_artifact:
        try:
            p = _artifact_path(skill_name, run_id)
            p.write_text(
                json.dumps(result.to_dict(), indent=2), encoding="utf-8",
            )
            result.artifact_path = str(p)
        except OSError as e:
            result.notes.append(f"artifact write failed: {e}")

    return result


# ============================================================
# Apply (persistence — caller gates with y/N)
# ============================================================


def apply_best(result: GepaResult) -> skills_mod.Skill:
    """Persist GEPA's chosen body to the skill on disk. Atomic via skills.save.

    Raises ``ValueError`` if the result has no candidate (no_signal) or if
    the skill no longer exists on disk. The caller MUST have shown the diff
    and gotten explicit y/N before calling this — apply_best does not gate.
    """
    if result.recommendation == "no_signal":
        raise ValueError("GEPA result has no candidate to apply (no_signal)")
    skill = skills_mod.load(result.skill_name)
    if skill is None:
        raise ValueError(f"skill '{result.skill_name}' no longer exists")
    skill.body = result.best.body.strip()
    skills_mod.save(skill)

    # Audit log if available.
    try:
        from . import audit_log
        audit_log.record(
            "skill.gepa.apply",
            name=skill.name,
            run_id=result.run_id,
            improvement=result.improvement,
            baseline_fitness=result.baseline.fitness,
            best_fitness=result.best.fitness,
        )
    except Exception:
        pass

    return skill


__all__ = [
    "Variant",
    "Generation",
    "GepaResult",
    "collect_records",
    "evolve",
    "apply_best",
]
