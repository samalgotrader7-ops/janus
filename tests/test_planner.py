from janus import planner, config


def test_topo_order_simple():
    a = planner.PlanNode(id="a", goal="x")
    b = planner.PlanNode(id="b", goal="y", deps=["a"])
    c = planner.PlanNode(id="c", goal="z", deps=["b"])
    out = planner.topo_order([c, a, b])
    assert [n.id for n in out] == ["a", "b", "c"]


def test_topo_order_independent():
    a = planner.PlanNode(id="a", goal="x")
    b = planner.PlanNode(id="b", goal="y")
    out = planner.topo_order([b, a])
    assert {n.id for n in out} == {"a", "b"}


def test_topo_order_cycle_does_not_crash():
    a = planner.PlanNode(id="a", goal="x", deps=["b"])
    b = planner.PlanNode(id="b", goal="y", deps=["a"])
    out = planner.topo_order([a, b])
    assert {n.id for n in out} == {"a", "b"}


def test_is_trivial():
    leaf = planner.PlanNode(id="main", goal="x")
    root = planner.PlanNode(id="root", goal="g", children=[leaf])
    assert planner.is_trivial(root)
    root.children.append(planner.PlanNode(id="other", goal="y"))
    assert not planner.is_trivial(root)


def test_render_indents():
    root = planner.PlanNode(
        id="root", goal="g",
        children=[
            planner.PlanNode(id="a", goal="alpha"),
            planner.PlanNode(id="b", goal="beta", deps=["a"], skill="s"),
        ],
    )
    out = planner.render(root)
    assert "alpha" in out
    assert "[skill=s]" in out
    assert "[deps=a]" in out


def test_plan_via_llm(janus_home, fake_llm):
    fake_llm.append({
        "content": (
            '{"goal": "Build X", "children": ['
            '{"id": "a", "goal": "scaffold", "skill": null, "deps": []},'
            '{"id": "b", "goal": "wire it",  "skill": null, "deps": ["a"]}'
            "]}"
        )
    })
    root = planner.plan("Build X agent", available_skills=[])
    assert root.goal == "Build X"
    assert len(root.children) == 2
    assert root.children[1].deps == ["a"]


def test_plan_respects_max_fanout(janus_home, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "PLAN_MAX_FANOUT", 2)
    fake_llm.append({
        "content": (
            '{"goal": "g", "children": ['
            '{"id":"a","goal":"x"},'
            '{"id":"b","goal":"y"},'
            '{"id":"c","goal":"z"},'
            '{"id":"d","goal":"w"}'
            "]}"
        )
    })
    root = planner.plan("do stuff")
    assert len(root.children) == 2
