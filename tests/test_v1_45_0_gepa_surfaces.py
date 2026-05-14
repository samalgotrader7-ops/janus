"""Tests for v1.45.0 — GEPA surfaces (CLI subcommand, MCP tool, render).

These tests verify the WIRING — that the slash subcommand parses,
that the MCP tool is registered with the expected schema, and that
the render_result helper produces the expected sections. The full
evolve() loop is exercised by test_v1_44_0_skill_gepa.py.
"""

from __future__ import annotations

import json

import pytest

from janus import config, skill_gepa, skills as skills_mod
from janus.tools.capabilities import CapabilitySet
from janus.mcp import server as mcp_server


# ============================================================
# render_result
# ============================================================


def _make_result(skill_name="t", best_id="g0_rewrite_1",
                 baseline_fitness=40.0, best_fitness=80.0, recommendation="apply"):
    baseline = skill_gepa.Variant(
        id="baseline", body="old body", fitness=baseline_fitness,
        operator="baseline", generation=-1,
    )
    best = skill_gepa.Variant(
        id=best_id, body="new body", fitness=best_fitness,
        operator="rewrite", parents=["baseline"], generation=0,
    )
    return skill_gepa.GepaResult(
        skill_name=skill_name,
        run_id="abc123",
        started_at="2026-05-14T00:00:00+00:00",
        ended_at="2026-05-14T00:01:00+00:00",
        config={
            "population": 4, "generations": 2, "record_count": 3,
            "budget_remaining": 100, "max_llm_calls": 200, "seed": None,
        },
        baseline=baseline,
        generations=[],
        best=best,
        improvement=best_fitness - baseline_fitness,
        recommendation=recommendation,
        artifact_path="/tmp/x.json",
    )


def test_render_result_includes_key_fields():
    result = _make_result()
    text = skill_gepa.render_result(result)
    assert "GEPA run abc123" in text
    assert "skill 't'" in text
    assert "baseline fitness=40.0" in text
    assert "best fitness=80.0" in text
    assert "improvement=+40.0" in text
    assert "recommendation: apply" in text
    assert "--- current body ---" in text
    assert "--- proposed body ---" in text
    assert "old body" in text
    assert "new body" in text


def test_render_result_no_diff_when_requested():
    result = _make_result()
    text = skill_gepa.render_result(result, include_diff=False)
    assert "--- current body ---" not in text
    assert "--- proposed body ---" not in text


def test_render_result_no_diff_when_best_is_baseline():
    """When GEPA found no improvement, the 'diff' is the baseline twice —
    suppress it for visual clarity."""
    baseline = skill_gepa.Variant(
        id="baseline", body="same", fitness=50.0, operator="baseline",
    )
    result = skill_gepa.GepaResult(
        skill_name="x", run_id="r", started_at="t", ended_at="t",
        config={"population": 4, "generations": 2, "record_count": 1,
                "budget_remaining": 100},
        baseline=baseline,
        generations=[],
        best=baseline,
        improvement=0.0,
        recommendation="no_change",
    )
    text = skill_gepa.render_result(result)
    assert "--- current body ---" not in text
    assert "recommendation: no_change" in text


def test_render_result_shows_notes():
    baseline = skill_gepa.Variant(
        id="baseline", body="", fitness=0.0, operator="baseline",
    )
    result = skill_gepa.GepaResult(
        skill_name="x", run_id="r", started_at="t", ended_at="t",
        config={"population": 0, "generations": 0, "record_count": 0,
                "budget_remaining": 0},
        baseline=baseline, generations=[], best=baseline,
        improvement=0.0, recommendation="no_signal",
        notes=["skill 'x' not found"],
    )
    text = skill_gepa.render_result(result)
    assert "notes:" in text
    assert "not found" in text


# ============================================================
# MCP tool wiring
# ============================================================


def test_mcp_tool_registered():
    assert "janus_skill_gepa" in mcp_server._TOOLS
    desc, schema, handler = mcp_server._TOOLS["janus_skill_gepa"]
    assert callable(handler)
    assert "skill" in schema["required"]
    assert "skill" in schema["properties"]
    assert "apply" in schema["properties"]


def test_mcp_tool_missing_skill_returns_error():
    desc, schema, handler = mcp_server._TOOLS["janus_skill_gepa"]
    out = handler({})
    assert out.startswith("error:")


def test_mcp_tool_routes_to_evolve(tmp_path, monkeypatch):
    """The handler must build kwargs from args and pass through evolve()."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)

    captured: dict = {}

    fake_result = _make_result(skill_name="probe", recommendation="no_change",
                               baseline_fitness=50.0, best_fitness=51.0)

    def fake_evolve(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs
        return fake_result

    monkeypatch.setattr(skill_gepa, "evolve", fake_evolve)

    desc, schema, handler = mcp_server._TOOLS["janus_skill_gepa"]
    out = handler({
        "skill": "probe",
        "generations": 5,
        "population": 8,
        "max_llm_calls": 99,
        "seed": 1234,
    })

    assert captured["name"] == "probe"
    assert captured["kwargs"]["generations"] == 5
    assert captured["kwargs"]["population"] == 8
    assert captured["kwargs"]["max_llm_calls"] == 99
    assert captured["kwargs"]["seed"] == 1234
    assert "recommendation: no_change" in out


def test_mcp_tool_apply_flag_persists(tmp_path, monkeypatch):
    """apply=true + recommendation=apply → MCP tool writes the new body."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)

    # Persist a real skill so apply_best can write back.
    skill = skills_mod.Skill(
        name="apply-probe",
        description="desc",
        state="trusted-auto",
        capabilities=CapabilitySet.from_dict({}),
        body="old",
        path=skills_dir / "apply-probe.md",
        raw_frontmatter={},
        created="2026-01-01T00:00:00+00:00",
        last_promoted=None,
        runs=1, success=1, fail=0,
    )
    skills_mod.save(skill)

    fake_result = _make_result(
        skill_name="apply-probe", recommendation="apply",
        baseline_fitness=40.0, best_fitness=90.0,
    )

    monkeypatch.setattr(skill_gepa, "evolve", lambda name, **kw: fake_result)

    desc, schema, handler = mcp_server._TOOLS["janus_skill_gepa"]
    out = handler({"skill": "apply-probe", "apply": True})
    assert "applied" in out

    reloaded = skills_mod.load("apply-probe")
    assert reloaded is not None
    assert reloaded.body == "new body"


def test_mcp_tool_apply_flag_respects_no_change(tmp_path, monkeypatch):
    """apply=true + recommendation=no_change → MCP tool refuses politely."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)

    fake_result = _make_result(recommendation="no_change",
                               baseline_fitness=50.0, best_fitness=51.0)
    monkeypatch.setattr(skill_gepa, "evolve", lambda name, **kw: fake_result)

    desc, schema, handler = mcp_server._TOOLS["janus_skill_gepa"]
    out = handler({"skill": "anything", "apply": True})
    assert "apply-skipped" in out


# ============================================================
# Slash dispatch advertised
# ============================================================


def test_slash_command_advertises_gepa():
    from janus import slash_dispatch
    skill_cmd = [c for c in slash_dispatch.BUILTIN_COMMANDS if c.name == "/skill"]
    assert skill_cmd, "/skill command not registered"
    assert "gepa" in skill_cmd[0].description
