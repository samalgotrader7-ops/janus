"""Orchestrator tests — uses fake_llm to stub planner+executor LLM calls."""
import json

from janus import orchestrator, planner, config


def _always_approve(*a, **kw):
    return True


def test_trivial_plan_matches_linear(janus_home, fake_llm, monkeypatch):
    """Trivial plan should produce one leaf identical to linear executor."""
    # Trivial plan: just a root with one leaf, no children.
    root = planner.PlanNode(id="root", goal="do thing",
                            children=[planner.PlanNode(id="main", goal="do thing")])

    # Stub a single executor LLM call returning a final answer (no tool calls).
    fake_llm.append({"content": "trivial output", "tool_calls": []})
    rr = orchestrator.run(
        original_request="do thing",
        chosen_label="alpha",
        chosen_action="do thing",
        plan=root,
        base_approver=_always_approve,
    )
    assert rr.plan_was_trivial
    assert len(rr.leaves) == 1
    assert rr.final_output == "trivial output"


def test_multi_leaf_plan_runs_in_dep_order(janus_home, fake_llm):
    a = planner.PlanNode(id="a", goal="alpha")
    b = planner.PlanNode(id="b", goal="beta", deps=["a"])
    root = planner.PlanNode(id="root", goal="combo", children=[a, b])

    # Two executor calls (one per leaf) + one summarizer call.
    fake_llm.append({"content": "alpha done", "tool_calls": []})
    fake_llm.append({"content": "beta done",  "tool_calls": []})
    fake_llm.append({"content": "summary: both done"})

    rr = orchestrator.run(
        original_request="do combo",
        chosen_label="combo",
        chosen_action="do combo",
        plan=root,
        base_approver=_always_approve,
    )
    assert not rr.plan_was_trivial
    assert [lr.id for lr in rr.leaves] == ["a", "b"]
    assert rr.leaves[0].output == "alpha done"
    assert rr.leaves[1].output == "beta done"
    assert "summary" in rr.final_output
