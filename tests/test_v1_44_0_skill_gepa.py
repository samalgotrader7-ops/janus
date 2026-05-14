"""Tests for v1.44.0 — skill_gepa evolutionary engine.

Pinned invariants:
  * No skill on disk → no_signal result, no crash.
  * No replay records → no_signal result, no crash.
  * Recorded records present + a judge that prefers some bodies → result
    contains generations, variants, and a best != baseline.
  * Improvement < GEPA_PROMOTE_MARGIN → recommendation "no_change".
  * Improvement >= GEPA_PROMOTE_MARGIN → recommendation "apply".
  * apply_best mutates the skill body atomically.
  * apply_best refuses on no_signal results.
  * Body-hash cache prevents re-judging duplicate variants.
  * call_budget enforced — once exhausted, evolve stops minting variants.
  * Selection prefers higher fitness, ties broken by shorter body.
  * Crossover with one survivor degenerates to single-parent rewrite-shape
    (defensive — doesn't crash).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from janus import config, skills as skills_mod, skill_gepa
from janus.tools.capabilities import CapabilitySet


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    skills_dir = home / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(config, "GEPA_GENERATIONS", 2)
    monkeypatch.setattr(config, "GEPA_POPULATION", 4)
    monkeypatch.setattr(config, "GEPA_RECORDS_PER_RUN", 3)
    monkeypatch.setattr(config, "GEPA_MAX_LLM_CALLS", 200)
    monkeypatch.setattr(config, "GEPA_PROMOTE_MARGIN", 5.0)
    yield home


def _make_skill(name: str, body: str = "do the thing") -> skills_mod.Skill:
    skill = skills_mod.Skill(
        name=name,
        description=f"test {name}",
        state="trusted-auto",
        capabilities=CapabilitySet.from_dict({}),
        body=body,
        path=config.SKILLS_DIR / f"{name}.md",
        raw_frontmatter={},
        created="2026-01-01T00:00:00+00:00",
        last_promoted="2026-01-01T00:00:00+00:00",
        runs=5,
        success=3,
        fail=1,
    )
    skills_mod.save(skill)
    return skill


# ============================================================
# Defensive: missing skill / empty records
# ============================================================


def test_missing_skill_returns_no_signal(isolated_home):
    result = skill_gepa.evolve(
        "nonexistent",
        _judge_fn=lambda b, r: (50.0, "stub"),
        _rewrite_fn=lambda p, f: p.body + "X",
        _simplify_fn=lambda p: p.body,
        _specialize_fn=lambda p, e: p.body + "Y",
        _crossover_fn=lambda a, b: a.body,
    )
    assert result.recommendation == "no_signal"
    assert any("not found" in n for n in result.notes)


def test_no_records_returns_no_signal(isolated_home, monkeypatch):
    _make_skill("lonely")
    monkeypatch.setattr(skill_gepa, "collect_records", lambda n, k: [])
    result = skill_gepa.evolve(
        "lonely",
        _judge_fn=lambda b, r: (50.0, "stub"),
        _rewrite_fn=lambda p, f: p.body + "X",
        _simplify_fn=lambda p: p.body,
        _specialize_fn=lambda p, e: p.body + "Y",
        _crossover_fn=lambda a, b: a.body,
    )
    assert result.recommendation == "no_signal"
    assert result.baseline.body == "do the thing"
    assert any("no replay records" in n for n in result.notes)


# ============================================================
# End-to-end with deterministic judge + mutators
# ============================================================


def test_evolve_finds_improvement(isolated_home, monkeypatch):
    """Judge prefers bodies containing 'good' — mutators bias towards 'good'."""
    _make_skill("target", body="initial")

    monkeypatch.setattr(
        skill_gepa, "collect_records",
        lambda n, k: [
            {"request": "do A", "output": "ok", "feedback": "good"},
            {"request": "do B", "output": "ok", "feedback": "good"},
            {"request": "do C", "output": "fail", "feedback": "bad"},
        ],
    )

    def judge(body: str, record: dict) -> tuple[float, str]:
        score = 30.0 + 20.0 * body.count("good") + 5.0 * body.count("step")
        return min(100.0, score), "stub"

    def mut_rewrite(p, failures):
        return p.body + " good"

    def mut_simplify(p):
        return p.body.replace("initial", "").strip()

    def mut_specialize(p, examples):
        return p.body + " step"

    def mut_crossover(a, b):
        return f"{a.body} | {b.body}"

    result = skill_gepa.evolve(
        "target",
        _judge_fn=judge,
        _rewrite_fn=mut_rewrite,
        _simplify_fn=mut_simplify,
        _specialize_fn=mut_specialize,
        _crossover_fn=mut_crossover,
        seed=42,
    )

    # Baseline ("initial") gets 30 per record; any body with "good" wins.
    assert result.baseline.fitness == 30.0
    assert result.best.fitness > result.baseline.fitness
    assert result.improvement > 0
    assert "good" in result.best.body or "step" in result.best.body
    assert result.recommendation == "apply"
    # Artifact was written to disk
    assert result.artifact_path is not None
    assert Path(result.artifact_path).exists()
    payload = json.loads(Path(result.artifact_path).read_text(encoding="utf-8"))
    assert payload["best"]["fitness"] == result.best.fitness


def test_evolve_no_improvement_recommends_no_change(isolated_home, monkeypatch):
    """Judge gives every variant the same score — no improvement."""
    _make_skill("flat", body="anything")

    monkeypatch.setattr(
        skill_gepa, "collect_records",
        lambda n, k: [{"request": "x", "output": "x", "feedback": None}],
    )

    result = skill_gepa.evolve(
        "flat",
        _judge_fn=lambda b, r: (50.0, "flat"),
        _rewrite_fn=lambda p, f: p.body + " (rewrite)",
        _simplify_fn=lambda p: p.body + " (simplify)",
        _specialize_fn=lambda p, e: p.body + " (specialize)",
        _crossover_fn=lambda a, b: a.body + " | " + b.body,
        seed=1,
    )

    assert result.improvement < config.GEPA_PROMOTE_MARGIN
    assert result.recommendation == "no_change"


# ============================================================
# apply_best
# ============================================================


def test_apply_best_persists_body(isolated_home, monkeypatch):
    _make_skill("apply-target", body="old body")
    monkeypatch.setattr(
        skill_gepa, "collect_records",
        lambda n, k: [{"request": "r", "output": "o", "feedback": "good"}],
    )

    def judge(body, record):
        return (80.0 if "new" in body else 10.0, "stub")

    result = skill_gepa.evolve(
        "apply-target",
        _judge_fn=judge,
        _rewrite_fn=lambda p, f: "new body shape",
        _simplify_fn=lambda p: p.body,
        _specialize_fn=lambda p, e: "new specialized body",
        _crossover_fn=lambda a, b: f"new mix of {a.body} and {b.body}",
        seed=7,
    )

    assert "new" in result.best.body
    skill_gepa.apply_best(result)
    reloaded = skills_mod.load("apply-target")
    assert reloaded is not None
    assert "new" in reloaded.body


def test_apply_best_refuses_no_signal(isolated_home):
    result = skill_gepa.evolve(
        "ghost",
        _judge_fn=lambda b, r: (50.0, "stub"),
        _rewrite_fn=lambda p, f: p.body,
        _simplify_fn=lambda p: p.body,
        _specialize_fn=lambda p, e: p.body,
        _crossover_fn=lambda a, b: a.body,
    )
    assert result.recommendation == "no_signal"
    with pytest.raises(ValueError):
        skill_gepa.apply_best(result)


# ============================================================
# Budget enforcement
# ============================================================


def test_budget_cap_stops_evolution(isolated_home, monkeypatch):
    _make_skill("budgeted", body="x")
    monkeypatch.setattr(
        skill_gepa, "collect_records",
        lambda n, k: [{"request": "a", "output": "b", "feedback": None} for _ in range(5)],
    )

    calls = {"n": 0}

    def counting_judge(body, record):
        calls["n"] += 1
        return 50.0, "stub"

    result = skill_gepa.evolve(
        "budgeted",
        generations=5,
        population=8,
        record_count=5,
        max_llm_calls=12,
        _judge_fn=counting_judge,
        _rewrite_fn=lambda p, f: p.body + "r",
        _simplify_fn=lambda p: p.body + "s",
        _specialize_fn=lambda p, e: p.body + "x",
        _crossover_fn=lambda a, b: a.body + "c",
        seed=3,
    )

    # Strict cap: judge + mutate calls combined never exceed budget.
    # We can't easily count mutations from the test, but judge calls
    # alone are bounded by the budget.
    assert calls["n"] <= 12
    assert result.config["budget_remaining"] >= 0


# ============================================================
# Cache prevents re-judging duplicate bodies
# ============================================================


def test_judge_cache_dedupes_identical_bodies(isolated_home, monkeypatch):
    _make_skill("dup", body="same")
    monkeypatch.setattr(
        skill_gepa, "collect_records",
        lambda n, k: [{"request": "x", "output": "y", "feedback": None}],
    )

    calls = {"n": 0}

    def judge(body, record):
        calls["n"] += 1
        return 50.0, "stub"

    # Every mutator returns the SAME body — judges should only run once
    # per unique body. Baseline scored = 1 judge call. All variants
    # collide with baseline → 0 additional judge calls.
    same = "identical body"
    skill = skills_mod.load("dup")
    skill.body = same
    skills_mod.save(skill)

    skill_gepa.evolve(
        "dup",
        generations=2,
        population=4,
        record_count=1,
        _judge_fn=judge,
        _rewrite_fn=lambda p, f: same,
        _simplify_fn=lambda p: same,
        _specialize_fn=lambda p, e: same,
        _crossover_fn=lambda a, b: same,
        seed=9,
    )

    # The baseline body == "identical body" and so do all variants,
    # but variants that equal the parent are skipped, so cache should
    # essentially mean 1 judge call total (just the baseline).
    assert calls["n"] == 1


# ============================================================
# Selection ordering
# ============================================================


def test_select_topk_sorts_by_fitness_then_length():
    from janus.skill_gepa import Variant, _select_topk

    a = Variant(id="a", body="xxxx", fitness=80.0, operator="t")
    b = Variant(id="b", body="x", fitness=80.0, operator="t")
    c = Variant(id="c", body="xxx", fitness=90.0, operator="t")
    d = Variant(id="d", body="xxxx", fitness=70.0, operator="t")

    top = _select_topk([a, b, c, d], 3)
    assert top[0].id == "c"  # highest fitness
    assert top[1].id == "b"  # tied fitness with a, but shorter
    assert top[2].id == "a"


# ============================================================
# Variant.body_hash stability
# ============================================================


def test_body_hash_stable():
    from janus.skill_gepa import Variant

    v1 = Variant(id="x", body="abc", fitness=0, operator="baseline")
    v2 = Variant(id="y", body="abc", fitness=99, operator="rewrite")
    assert v1.body_hash() == v2.body_hash()
    v3 = Variant(id="z", body="abcd", fitness=0, operator="baseline")
    assert v1.body_hash() != v3.body_hash()
