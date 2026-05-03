"""Tests for swarms.spec — parser + Validator + input validation."""
from __future__ import annotations
from pathlib import Path

import pytest

from janus.swarms import spec


FIXTURE_DIR = Path(__file__).parent / "fixtures"
DEMO_PATH = FIXTURE_DIR / "demo-swarm.md"


# ---------- Validator ----------


def test_validator_required_present():
    v = spec.Validator({"name": "x"}, where="root")
    assert v.required("name", str) == "x"


def test_validator_required_missing():
    v = spec.Validator({}, where="root")
    with pytest.raises(spec.SpecError, match="missing required field: root.name"):
        v.required("name", str)


def test_validator_required_wrong_type():
    v = spec.Validator({"name": 123}, where="root")
    with pytest.raises(spec.SpecError, match="must be str"):
        v.required("name", str)


def test_validator_optional_default():
    v = spec.Validator({}, where="root")
    assert v.optional("count", int, default=10) == 10


def test_validator_optional_present():
    v = spec.Validator({"count": 7}, where="root")
    assert v.optional("count", int, default=10) == 7


def test_validator_optional_min_max():
    v = spec.Validator({"count": 5}, where="root")
    with pytest.raises(spec.SpecError, match="must be >= 10"):
        v.optional("count", int, min_=10)
    v2 = spec.Validator({"count": 100}, where="root")
    with pytest.raises(spec.SpecError, match="must be <= 50"):
        v2.optional("count", int, max_=50)


def test_validator_optional_none_returns_default():
    v = spec.Validator({"count": None}, where="root")
    assert v.optional("count", int, default=10) == 10


def test_validator_enum_valid():
    v = spec.Validator({"mode": "fast"}, where="root")
    assert v.enum("mode", choices=("fast", "slow")) == "fast"


def test_validator_enum_invalid():
    v = spec.Validator({"mode": "weird"}, where="root")
    with pytest.raises(spec.SpecError, match="must be one of"):
        v.enum("mode", choices=("fast", "slow"))


def test_validator_enum_default_none():
    v = spec.Validator({}, where="root")
    assert v.enum("mode", choices=("fast", "slow")) is None


def test_validator_dict_default_empty():
    v = spec.Validator({}, where="root")
    assert v.dict("budget") == {}


def test_validator_dict_wrong_type():
    v = spec.Validator({"budget": "not-a-dict"}, where="root")
    with pytest.raises(spec.SpecError, match="must be a dict"):
        v.dict("budget")


def test_validator_list_default_empty():
    v = spec.Validator({}, where="root")
    assert v.list("phases") == []


def test_validator_list_wrong_type():
    v = spec.Validator({"phases": "nope"}, where="root")
    with pytest.raises(spec.SpecError, match="must be a list"):
        v.list("phases")


def test_validator_required_tuple_of_types():
    v = spec.Validator({"x": 3.14}, where="root")
    assert v.required("x", (int, float)) == 3.14


# ---------- parse_spec / load_spec ----------


def test_load_demo_spec():
    s = spec.load_spec(DEMO_PATH)
    assert s.name == "demo-swarm"
    assert s.version == 1
    assert "Generic 2-phase demo" in s.description
    assert s.output_format == "json"
    assert s.path == DEMO_PATH


def test_demo_budget_parsed():
    s = spec.load_spec(DEMO_PATH)
    assert s.budget.max_usd == 0.10
    assert s.budget.max_wallclock_s == 60
    assert s.budget.max_subagents == 5
    assert s.budget.max_recursion_depth == 1
    assert s.budget.max_total_tool_calls == 20
    assert s.budget.max_completion_tokens_per_role == 200


def test_demo_inputs_parsed():
    s = spec.load_spec(DEMO_PATH)
    by_name = {i.name: i for i in s.inputs}
    assert "count" in by_name
    assert "label" in by_name
    assert by_name["count"].type == "int"
    assert by_name["count"].required is True
    assert by_name["count"].min == 1
    assert by_name["count"].max == 5
    assert by_name["label"].type == "string"
    assert by_name["label"].required is False
    assert by_name["label"].default == "demo"


def test_demo_permissions_parsed():
    s = spec.load_spec(DEMO_PATH)
    assert s.permissions.default_mode == "plan"
    assert s.permissions.per_role == {"reporter": "acceptEdits"}


def test_demo_phases_parsed():
    s = spec.load_spec(DEMO_PATH)
    assert len(s.phases) == 2
    p0, p1 = s.phases
    assert p0.name == "collect"
    assert p0.pattern == "map_reduce"
    assert p0.role == "collector"
    assert p0.model == "test-model-cheap"
    assert p0.tool_names == ["add_one"]
    assert p0.capabilities == {"math.compute": ["*"]}
    assert p0.aggregator == "concat"
    assert p0.depends_on is None

    assert p1.name == "report"
    assert p1.pattern == "single"
    assert p1.role == "reporter"
    assert p1.model == "test-model-strong"
    assert p1.aggregator == "llm_summarize"
    assert p1.aggregator_args == {"template": "Summarize {n} collected values."}
    assert p1.depends_on == "collect"


def test_demo_body_intact():
    s = spec.load_spec(DEMO_PATH)
    assert "You are the {role} sub-agent" in s.body
    assert "{spec_name}" in s.body


# ---------- Errors ----------


def test_missing_frontmatter_rejected():
    with pytest.raises(spec.SpecError, match="missing YAML frontmatter"):
        spec.parse_spec("body only, no frontmatter")


def test_wrong_type_field_rejected():
    text = """---
name: thing
type: skill
---
body
"""
    with pytest.raises(spec.SpecError, match="type must be 'swarm'"):
        spec.parse_spec(text)


def test_bad_name_rejected():
    text = """---
name: NotKebab
type: swarm
phases:
  p:
    pattern: single
    role: r
    aggregator: concat
---
body
"""
    with pytest.raises(spec.SpecError, match="name must be kebab-case"):
        spec.parse_spec(text)


def test_zero_phases_rejected():
    text = """---
name: empty
type: swarm
---
body
"""
    with pytest.raises(spec.SpecError, match="at least one phase"):
        spec.parse_spec(text)


def test_invalid_aggregator_rejected():
    text = """---
name: bad-agg
type: swarm
phases:
  p:
    pattern: single
    role: r
    aggregator: not_a_real_aggregator
---
body
"""
    with pytest.raises(spec.SpecError, match="aggregator must be one of"):
        spec.parse_spec(text)


def test_invalid_pattern_rejected():
    text = """---
name: bad-pattern
type: swarm
phases:
  p:
    pattern: weird
    role: r
    aggregator: concat
---
body
"""
    with pytest.raises(spec.SpecError, match="pattern must be one of"):
        spec.parse_spec(text)


def test_invalid_default_mode_rejected():
    text = """---
name: bad-mode
type: swarm
permissions:
  default_mode: unknown_mode
phases:
  p:
    pattern: single
    role: r
    aggregator: concat
---
body
"""
    with pytest.raises(spec.SpecError, match="default_mode must be one of"):
        spec.parse_spec(text)


def test_invalid_per_role_mode_rejected():
    text = """---
name: bad-per-role
type: swarm
permissions:
  per_role:
    reporter: weird_mode
phases:
  p:
    pattern: single
    role: r
    aggregator: concat
---
body
"""
    with pytest.raises(spec.SpecError, match="per_role.reporter must be one of"):
        spec.parse_spec(text)


def test_invalid_input_type_rejected():
    text = """---
name: bad-input-type
type: swarm
inputs:
  thing:
    type: dictionary
phases:
  p:
    pattern: single
    role: r
    aggregator: concat
---
body
"""
    with pytest.raises(spec.SpecError, match="type must be one of"):
        spec.parse_spec(text)


def test_depends_on_unknown_phase_rejected():
    text = """---
name: bad-deps
type: swarm
phases:
  p1:
    pattern: single
    role: r
    aggregator: concat
    depends_on: nonexistent
---
body
"""
    with pytest.raises(spec.SpecError, match="references unknown phase"):
        spec.parse_spec(text)


def test_depends_on_later_phase_rejected():
    text = """---
name: bad-deps-order
type: swarm
phases:
  p1:
    pattern: single
    role: r
    aggregator: concat
    depends_on: p2
  p2:
    pattern: single
    role: r
    aggregator: concat
---
body
"""
    with pytest.raises(spec.SpecError, match="must reference an EARLIER phase"):
        spec.parse_spec(text)


def test_depends_on_self_rejected():
    text = """---
name: bad-self-dep
type: swarm
phases:
  p1:
    pattern: single
    role: r
    aggregator: concat
    depends_on: p1
---
body
"""
    with pytest.raises(spec.SpecError, match="must reference an EARLIER phase"):
        spec.parse_spec(text)


def test_budget_above_ceiling_rejected():
    # SWARM_MAX_SUBAGENTS default is 30; ask for 1000
    text = """---
name: too-greedy
type: swarm
budget:
  max_subagents: 1000
phases:
  p:
    pattern: single
    role: r
    aggregator: concat
---
body
"""
    with pytest.raises(spec.SpecError, match="must be <="):
        spec.parse_spec(text)


# ---------- find_spec / list_specs ----------


def test_find_spec_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "janus.config.SWARM_SPECS_DIR", tmp_path,
    )
    (tmp_path / "demo-swarm.md").write_text(
        DEMO_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    found = spec.find_spec("demo-swarm")
    assert found is not None
    assert found.name == "demo-swarm"


def test_find_spec_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("janus.config.SWARM_SPECS_DIR", tmp_path)
    assert spec.find_spec("nope") is None


def test_list_specs_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("janus.config.SWARM_SPECS_DIR", tmp_path)
    assert spec.list_specs() == []


def test_list_specs_skips_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr("janus.config.SWARM_SPECS_DIR", tmp_path)
    (tmp_path / "good.md").write_text(
        DEMO_PATH.read_text(encoding="utf-8").replace("demo-swarm", "good"),
        encoding="utf-8",
    )
    (tmp_path / "broken.md").write_text("no frontmatter here", encoding="utf-8")
    items = spec.list_specs()
    assert len(items) == 1
    assert items[0].name == "good"


def test_list_specs_no_directory(tmp_path, monkeypatch):
    nonexistent = tmp_path / "nope"
    monkeypatch.setattr("janus.config.SWARM_SPECS_DIR", nonexistent)
    assert spec.list_specs() == []


# ---------- validate_inputs ----------


def test_validate_inputs_happy():
    s = spec.load_spec(DEMO_PATH)
    out = spec.validate_inputs(s, {"count": 3})
    assert out == {"count": 3, "label": "demo"}


def test_validate_inputs_explicit_override_default():
    s = spec.load_spec(DEMO_PATH)
    out = spec.validate_inputs(s, {"count": 2, "label": "custom"})
    assert out == {"count": 2, "label": "custom"}


def test_validate_inputs_missing_required():
    s = spec.load_spec(DEMO_PATH)
    with pytest.raises(spec.SpecError, match="missing required input: count"):
        spec.validate_inputs(s, {})


def test_validate_inputs_wrong_type():
    s = spec.load_spec(DEMO_PATH)
    with pytest.raises(spec.SpecError, match="must be int"):
        spec.validate_inputs(s, {"count": "three"})


def test_validate_inputs_below_min():
    s = spec.load_spec(DEMO_PATH)
    with pytest.raises(spec.SpecError, match="must be >= 1"):
        spec.validate_inputs(s, {"count": 0})


def test_validate_inputs_above_max():
    s = spec.load_spec(DEMO_PATH)
    with pytest.raises(spec.SpecError, match="must be <= 5"):
        spec.validate_inputs(s, {"count": 100})


def test_validate_inputs_unknown_extras():
    s = spec.load_spec(DEMO_PATH)
    with pytest.raises(spec.SpecError, match="unknown inputs"):
        spec.validate_inputs(s, {"count": 1, "rogue": "value"})


def test_validate_inputs_bool_rejected_for_int():
    s = spec.load_spec(DEMO_PATH)
    # bool is a subclass of int — must be rejected explicitly to avoid
    # surprising True → 1 coercion.
    with pytest.raises(spec.SpecError):
        spec.validate_inputs(s, {"count": True})


# ---------- Defaults when sections omitted ----------


def test_minimal_spec_uses_defaults():
    text = """---
name: tiny
type: swarm
phases:
  only:
    pattern: single
    role: worker
    aggregator: concat
---
"""
    s = spec.parse_spec(text)
    assert s.budget.max_subagents == 10
    assert s.budget.max_usd == 5.0
    assert s.permissions.default_mode == "plan"
    assert s.output_format == "markdown"
    assert s.inputs == []
    assert len(s.phases) == 1


def test_phase_defaults():
    text = """---
name: tiny
type: swarm
phases:
  only:
    pattern: single
    role: worker
    aggregator: concat
---
"""
    s = spec.parse_spec(text)
    p = s.phases[0]
    assert p.name == "only"
    assert p.model is None
    assert p.tool_names == []
    assert p.capabilities == {}
    assert p.input_partition == "per_item"
    assert p.max_per_batch == 5
    assert p.depends_on is None
    assert p.aggregator_args == {}


def test_phases_preserve_declaration_order():
    text = """---
name: ordered
type: swarm
phases:
  z_first:
    pattern: single
    role: r
    aggregator: concat
  a_second:
    pattern: single
    role: r
    aggregator: concat
    depends_on: z_first
  m_third:
    pattern: single
    role: r
    aggregator: concat
    depends_on: a_second
---
"""
    s = spec.parse_spec(text)
    assert [p.name for p in s.phases] == ["z_first", "a_second", "m_third"]


def test_duplicate_phase_name_structurally_impossible():
    # YAML dict keys can't repeat — this is enforced by the YAML parser,
    # not by spec.py. Confirm a "duplicate" entry is silently overwritten
    # at the YAML layer (not our concern). We just verify the parser
    # doesn't crash on this input.
    text = """---
name: dup
type: swarm
phases:
  p:
    pattern: single
    role: r1
    aggregator: concat
  p:
    pattern: single
    role: r2
    aggregator: count
---
"""
    s = spec.parse_spec(text)
    assert len(s.phases) == 1
    # Last value wins per YAML semantics.
    assert s.phases[0].role == "r2"
