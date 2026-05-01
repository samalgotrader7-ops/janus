"""Tests for Phase 7 — skill evolution loop.

Covers:
- skills.record_run() counter persistence (atomic save).
- skill_evolution.should_propose threshold semantics.
- evolve_capabilities frontmatter opt-in.
- propose_revision LLM round-trip via fake_llm (no network).
- apply_revision body vs body+capabilities behavior.
- resolve_success heuristic + explicit override.
- eval.replay skill_filter.
"""
from __future__ import annotations
import json

from janus import skills, skill_evolution, eval as eval_mod, logger


# ---------- helpers ----------


_BASE_SKILL = """---
name: {name}
description: {desc}
state: quarantined
capabilities:
  shell.exec:
    - "git *"
  fs.read:
    - "**"
created: 2026-04-30T00:00:00Z
last-promoted: null
runs: {runs}
success: {success}
fail: {fail}
{extra}---

You are running {name}.

Steps:
1. step one
2. step two
"""


def _write_skill(janus_home, name, *, runs=0, success=0, fail=0, extra="", desc="A skill"):
    text = _BASE_SKILL.format(
        name=name, desc=desc, runs=runs, success=success, fail=fail, extra=extra,
    )
    d = janus_home / "skills"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return p


def _llm_response(payload: dict) -> dict:
    return {"role": "assistant", "content": json.dumps(payload)}


# ---------- record_run + persistence ----------


def test_record_run_increments_runs_and_success(janus_home):
    _write_skill(janus_home, "rev-skill")
    s1 = skills.record_run("rev-skill", success=True)
    assert s1 is not None
    assert s1.runs == 1 and s1.success == 1 and s1.fail == 0

    s2 = skills.record_run("rev-skill", success=True)
    assert s2.runs == 2 and s2.success == 2 and s2.fail == 0

    s3 = skills.record_run("rev-skill", success=False)
    assert s3.runs == 3 and s3.success == 2 and s3.fail == 1

    s4 = skills.record_run("rev-skill", success=None)
    assert s4.runs == 4 and s4.success == 2 and s4.fail == 1


def test_record_run_persists_to_disk(janus_home):
    _write_skill(janus_home, "rev-skill")
    skills.record_run("rev-skill", success=True)
    skills.record_run("rev-skill", success=False)

    # Reload from disk — counters survive an atomic save round trip.
    reloaded = skills.load("rev-skill")
    assert reloaded.runs == 2
    assert reloaded.success == 1
    assert reloaded.fail == 1


def test_record_run_missing_skill_returns_none(janus_home):
    # P8: errors are observations. record_run for an unknown skill must not raise.
    out = skills.record_run("does-not-exist", success=True)
    assert out is None


# ---------- should_propose ----------


def test_should_propose_at_threshold(janus_home):
    _write_skill(janus_home, "rev-skill", runs=5)
    s = skills.load("rev-skill")
    assert skill_evolution.should_propose(s) is True
    assert skill_evolution.should_propose(s, threshold=10) is False


def test_should_propose_only_at_multiples(janus_home):
    _write_skill(janus_home, "rev-skill", runs=4)
    s = skills.load("rev-skill")
    assert skill_evolution.should_propose(s) is False

    _write_skill(janus_home, "rev-skill", runs=10)
    s = skills.load("rev-skill")
    assert skill_evolution.should_propose(s) is True


def test_should_propose_zero_runs_is_false(janus_home):
    _write_skill(janus_home, "rev-skill", runs=0)
    s = skills.load("rev-skill")
    assert skill_evolution.should_propose(s) is False


# ---------- evolve_capabilities flag ----------


def test_evolve_capabilities_default_off(janus_home):
    _write_skill(janus_home, "rev-skill")
    s = skills.load("rev-skill")
    assert s.evolve_capabilities_enabled() is False


def test_evolve_capabilities_opt_in(janus_home):
    _write_skill(janus_home, "rev-skill", extra="evolve-capabilities: true\n")
    s = skills.load("rev-skill")
    assert s.evolve_capabilities_enabled() is True


# ---------- propose_revision (uses fake_llm) ----------


def test_propose_revision_no_change(janus_home, fake_llm):
    _write_skill(janus_home, "rev-skill")
    fake_llm.append(_llm_response({
        "changed": False,
        "rationale": "recent runs all succeeded; no signal to act on",
    }))
    s = skills.load("rev-skill")
    rev = skill_evolution.propose_revision(s, log_records=[])
    assert rev["changed"] is False
    assert "rationale" in rev


def test_propose_revision_body_change(janus_home, fake_llm):
    _write_skill(janus_home, "rev-skill")
    new_body = "You are running rev-skill.\n\nSteps:\n1. better step\n2. another\n"
    fake_llm.append(_llm_response({
        "changed": True,
        "rationale": "two failed runs both missed step 1; clarifying it",
        "body": new_body,
    }))
    s = skills.load("rev-skill")
    rev = skill_evolution.propose_revision(s, log_records=[])
    assert rev["changed"] is True
    # propose_revision strips whitespace; compare on stripped form.
    assert rev["body"] == new_body.strip()
    # No capabilities in proposal → key absent.
    assert "capabilities" not in rev


def test_propose_revision_strips_capabilities_when_not_opted_in(janus_home, fake_llm):
    """Even if the LLM tries to propose capabilities, propose_revision must
    drop them when the skill has not opted in via the frontmatter flag."""
    _write_skill(janus_home, "rev-skill")  # no evolve-capabilities flag
    fake_llm.append(_llm_response({
        "changed": True,
        "rationale": "expand caps please",
        "body": "new body",
        "capabilities": {"shell.exec": ["* "]},
    }))
    s = skills.load("rev-skill")
    rev = skill_evolution.propose_revision(s, log_records=[])
    assert rev["changed"] is True
    # Capabilities silently dropped because skill is not opted in.
    assert "capabilities" not in rev


def test_propose_revision_preserves_capabilities_when_opted_in(janus_home, fake_llm):
    _write_skill(janus_home, "rev-skill", extra="evolve-capabilities: true\n")
    fake_llm.append(_llm_response({
        "changed": True,
        "rationale": "tighten globs based on observed runs",
        "body": "new body",
        "capabilities": {"shell.exec": ["git status", "git log *"]},
    }))
    s = skills.load("rev-skill")
    rev = skill_evolution.propose_revision(s, log_records=[])
    assert rev["changed"] is True
    assert rev["capabilities"] == {"shell.exec": ["git status", "git log *"]}


def test_propose_revision_handles_unparseable_llm(janus_home, fake_llm):
    _write_skill(janus_home, "rev-skill")
    fake_llm.append({"role": "assistant", "content": "not json at all"})
    s = skills.load("rev-skill")
    rev = skill_evolution.propose_revision(s, log_records=[])
    assert rev["changed"] is False
    assert "rationale" in rev


def test_propose_revision_rejects_changed_with_empty_body(janus_home, fake_llm):
    _write_skill(janus_home, "rev-skill")
    fake_llm.append(_llm_response({"changed": True, "rationale": "x", "body": ""}))
    s = skills.load("rev-skill")
    rev = skill_evolution.propose_revision(s, log_records=[])
    assert rev["changed"] is False


# ---------- apply_revision ----------


def test_apply_revision_no_change_is_noop(janus_home):
    _write_skill(janus_home, "rev-skill")
    s = skills.load("rev-skill")
    original_body = s.body
    skill_evolution.apply_revision(s, {"changed": False, "rationale": "no"})
    reloaded = skills.load("rev-skill")
    assert reloaded.body == original_body


def test_apply_revision_body_only(janus_home):
    _write_skill(janus_home, "rev-skill")
    s = skills.load("rev-skill")
    skill_evolution.apply_revision(s, {
        "changed": True,
        "body": "Brand new body.\n\n1. step\n",
        "rationale": "x",
    })
    reloaded = skills.load("rev-skill")
    assert "Brand new body." in reloaded.body
    # Capabilities unchanged.
    assert reloaded.capabilities.grants("shell", "exec", "git status")


def test_apply_revision_ignores_capabilities_without_flag(janus_home):
    _write_skill(janus_home, "rev-skill")
    s = skills.load("rev-skill")
    skill_evolution.apply_revision(s, {
        "changed": True,
        "body": "new body",
        "capabilities": {"shell.exec": ["rm -rf *"]},
        "rationale": "trying to broaden caps",
    })
    reloaded = skills.load("rev-skill")
    # Caps did NOT change — flag was off.
    assert reloaded.capabilities.grants("shell", "exec", "git status")
    assert not reloaded.capabilities.grants("shell", "exec", "rm -rf /tmp/foo")


def test_apply_revision_applies_capabilities_when_flag_set(janus_home):
    _write_skill(janus_home, "rev-skill", extra="evolve-capabilities: true\n")
    s = skills.load("rev-skill")
    skill_evolution.apply_revision(s, {
        "changed": True,
        "body": "new body",
        "capabilities": {"shell.exec": ["git status"]},
        "rationale": "tighten",
    })
    reloaded = skills.load("rev-skill")
    # Tightened: only "git status" matches now.
    assert reloaded.capabilities.grants("shell", "exec", "git status")
    assert not reloaded.capabilities.grants("shell", "exec", "git log")


# ---------- resolve_success / infer_success ----------


def test_infer_success_clean_output():
    assert skill_evolution.infer_success("All done.\nResult: ok.", trace=[]) is True


def test_infer_success_error_marker_in_output():
    assert skill_evolution.infer_success("error: something broke", trace=[]) is False


def test_infer_success_empty_output():
    assert skill_evolution.infer_success("", trace=[]) is False


def test_infer_success_plan_leaf_error():
    trace = [{"leaf": "a", "trace": [], "error": None},
             {"leaf": "b", "trace": [], "error": "RuntimeError: X"}]
    assert skill_evolution.infer_success("everything is fine", trace=trace) is False


def test_resolve_success_explicit_overrides_heuristic():
    # Heuristic would say False; explicit "good" wins.
    assert skill_evolution.resolve_success(
        "error: nope", trace=[], explicit_feedback="good",
    ) is True
    # Heuristic would say True; explicit "bad" wins.
    assert skill_evolution.resolve_success(
        "great success", trace=[], explicit_feedback="bad",
    ) is False
    # No explicit → falls through to heuristic.
    assert skill_evolution.resolve_success(
        "great success", trace=[], explicit_feedback=None,
    ) is True


# ---------- eval skill_filter ----------


def test_eval_replay_filters_by_skill(janus_home, fake_llm):
    """eval.replay(skill_filter='X') only replays records tagged with X."""
    # Seed log with three records: two for skill-a, one for skill-b.
    for r in [
        {"ts": "2026-04-30T10:00:00Z", "request": "do A1", "skill": "skill-a",
         "interpretations": [{"label": "do a1", "action": "...", "risk": "low"}],
         "choice": 1},
        {"ts": "2026-04-30T11:00:00Z", "request": "do B1", "skill": "skill-b",
         "interpretations": [{"label": "do b1", "action": "...", "risk": "low"}],
         "choice": 1},
        {"ts": "2026-04-30T12:00:00Z", "request": "do A2", "skill": "skill-a",
         "interpretations": [{"label": "do a2", "action": "...", "risk": "low"}],
         "choice": 1},
    ]:
        logger.write(r)

    # Two replays of skill-a → fake_llm needs two interpretation responses.
    fake_llm.append(_llm_response({"interpretations": [
        {"label": "do a1", "action": "...", "risk": "low"},
    ]}))
    fake_llm.append(_llm_response({"interpretations": [
        {"label": "do a2", "action": "...", "risk": "low"},
    ]}))

    report = eval_mod.replay(last_n=10, skill_filter="skill-a", write_report=False)
    assert report.n_records == 2
    requests = [r.request for r in report.by_record]
    assert "do A1" in requests
    assert "do A2" in requests
    assert "do B1" not in requests


# ---------- recent_skill_runs ----------


def test_recent_skill_runs_filters_correctly(janus_home):
    for r in [
        {"ts": "t1", "request": "A1", "skill": "skill-a"},
        {"ts": "t2", "request": "B1", "skill": "skill-b"},
        {"ts": "t3", "request": "A2", "skill": "skill-a"},
        {"ts": "t4", "request": "no-skill"},
    ]:
        logger.write(r)
    out = skill_evolution.recent_skill_runs("skill-a")
    assert [r["request"] for r in out] == ["A1", "A2"]


# ---------- render_revision ----------


def test_render_revision_no_change_message(janus_home):
    _write_skill(janus_home, "rev-skill")
    s = skills.load("rev-skill")
    out = skill_evolution.render_revision(s, {
        "changed": False, "rationale": "all green",
    })
    assert "no change proposed" in out
    assert "all green" in out


def test_render_revision_includes_both_bodies(janus_home):
    _write_skill(janus_home, "rev-skill")
    s = skills.load("rev-skill")
    out = skill_evolution.render_revision(s, {
        "changed": True, "rationale": "x", "body": "NEW BODY HERE",
    })
    assert "current body" in out
    assert "proposed body" in out
    assert "NEW BODY HERE" in out
