"""
swarms/spec.py — markdown swarm spec parser + hand-rolled validator.

A swarm spec is a markdown file with YAML frontmatter declaring:
  - identity (name, version, description, type=swarm)
  - resource budget (USD, wallclock, sub-agent count, recursion depth,
    total tool calls, completion-tokens-per-role)
  - input schema (validated at swarm launch — bad input means $0 spent)
  - output format
  - permission policy (default mode + per-role overrides; cannot escalate
    beyond the parent swarm's mode)
  - phases (sequential list; each has role/model/tools/capabilities/aggregator)

The body is the system-prompt template per role with {placeholder}
interpolation handled by the runner at dispatch time.

P6 invariant — the validator is hand-rolled (no `jsonschema` dep). The
shape it covers (required/optional/enum/range) is small enough that a
full schema library would be net negative for the project's lean
dependency story.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..skills import parse_frontmatter


VALID_AGGREGATORS = (
    "concat", "dedupe_by", "count", "jsonl_merge", "topk", "llm_summarize",
)
VALID_PATTERNS = ("map_reduce", "single")
VALID_PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
VALID_OUTPUT_FORMATS = ("json", "jsonl", "markdown", "csv", "text")
VALID_INPUT_TYPES = ("string", "int", "float", "bool", "list")
VALID_INPUT_PARTITIONS = ("per_item", "regional_batches", "full")


_NAME_RX = re.compile(r"^[a-z][a-z0-9_-]*$")


# ---------- Data classes ----------


@dataclass
class Budget:
    max_usd: float = 5.0
    max_wallclock_s: int = 600
    max_subagents: int = 10
    max_recursion_depth: int = 2
    max_total_tool_calls: int = 200
    max_completion_tokens_per_role: int = 800


@dataclass
class InputDef:
    name: str
    type: str
    required: bool = False
    default: Any = None
    min: Any = None
    max: Any = None


@dataclass
class Phase:
    name: str
    pattern: str                                  # map_reduce | single
    role: str
    aggregator: str
    aggregator_args: dict = field(default_factory=dict)
    model: str | None = None                      # falls back to config.MODEL
    tool_names: list[str] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    input_partition: str = "per_item"
    max_per_batch: int = 5
    depends_on: str | None = None                  # earlier phase's name


@dataclass
class Permissions:
    default_mode: str = "plan"
    per_role: dict = field(default_factory=dict)


@dataclass
class Spec:
    name: str
    version: int
    description: str
    budget: Budget
    inputs: list[InputDef]
    output_format: str
    permissions: Permissions
    phases: list[Phase]
    body: str
    raw_frontmatter: dict
    path: Path | None = None


# ---------- Validator ----------


class SpecError(ValueError):
    """Raised when a swarm spec or launch input fails validation."""


@dataclass
class Validator:
    """Hand-rolled validator. Type hints are tuples-of-types or single types.

    Use:
        v = Validator(d, where="phase[0]")
        name = v.required("name", str)
        cnt = v.optional("count", int, default=10, min_=1, max_=100)
        pattern = v.enum("pattern", choices=("map_reduce", "single"))
        nested = v.dict("budget")
        items = v.list("phases")
    """
    d: dict
    where: str = ""

    def _label(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def required(self, key: str, type_: type | tuple) -> Any:
        if key not in self.d:
            raise SpecError(f"missing required field: {self._label(key)}")
        v = self.d[key]
        if not isinstance(v, type_):
            raise SpecError(
                f"{self._label(key)} must be {_typename(type_)}, "
                f"got {type(v).__name__}"
            )
        return v

    def optional(
        self,
        key: str,
        type_: type | tuple,
        default: Any = None,
        min_: Any = None,
        max_: Any = None,
    ) -> Any:
        if key not in self.d:
            return default
        v = self.d[key]
        if v is None:
            return default
        if not isinstance(v, type_):
            raise SpecError(
                f"{self._label(key)} must be {_typename(type_)}, "
                f"got {type(v).__name__}"
            )
        if min_ is not None and v < min_:
            raise SpecError(f"{self._label(key)} must be >= {min_}, got {v}")
        if max_ is not None and v > max_:
            raise SpecError(f"{self._label(key)} must be <= {max_}, got {v}")
        return v

    def enum(
        self, key: str, choices: tuple, default: Any = None,
    ) -> Any:
        v = self.d.get(key, default)
        if v is None:
            return None
        if v not in choices:
            raise SpecError(
                f"{self._label(key)} must be one of {choices}, got {v!r}"
            )
        return v

    def dict(self, key: str, default: dict | None = None) -> dict:
        v = self.d.get(key)
        if v is None:
            return default if default is not None else {}
        if not isinstance(v, dict):
            raise SpecError(
                f"{self._label(key)} must be a dict, got {type(v).__name__}"
            )
        return v

    def list(self, key: str, default: list | None = None) -> list:
        v = self.d.get(key)
        if v is None:
            return default if default is not None else []
        if not isinstance(v, list):
            raise SpecError(
                f"{self._label(key)} must be a list, got {type(v).__name__}"
            )
        return v


def _typename(t: type | tuple) -> str:
    if isinstance(t, tuple):
        return " or ".join(x.__name__ for x in t)
    return t.__name__


# ---------- Parser ----------


def parse_spec(text: str, *, path: Path | None = None) -> Spec:
    """Parse a swarm spec from markdown text. Raises SpecError on failure."""
    fm, body = parse_frontmatter(text)
    if not fm:
        raise SpecError("missing YAML frontmatter (--- blocks)")
    if fm.get("type") != "swarm":
        raise SpecError(f"type must be 'swarm', got {fm.get('type')!r}")

    v = Validator(fm)
    name = v.required("name", str)
    if not _NAME_RX.match(name):
        raise SpecError(
            f"name must be kebab-case (lowercase, hyphens, digits), got {name!r}"
        )

    phases = _parse_phases(v.dict("phases"), where="phases")
    if not phases:
        raise SpecError("phases: at least one phase required")

    # depends_on must reference an earlier phase (sequential, no cycles).
    # Phases are dict-of-dicts in YAML — insertion order is the run order
    # (Python 3.7+ dicts preserve insertion order).
    by_name = {p.name: i for i, p in enumerate(phases)}
    for i, p in enumerate(phases):
        if p.depends_on is None:
            continue
        if p.depends_on not in by_name:
            raise SpecError(
                f"phases[{p.name}].depends_on references unknown phase {p.depends_on!r}"
            )
        if by_name[p.depends_on] >= i:
            raise SpecError(
                f"phases[{p.name}].depends_on must reference an EARLIER phase "
                f"(got phases[{p.depends_on}] at position {by_name[p.depends_on]}, "
                f"current at position {i})"
            )

    return Spec(
        name=name,
        version=v.optional("version", int, default=1, min_=1),
        description=v.optional("description", str, default=""),
        budget=_parse_budget(v.dict("budget"), where="budget"),
        inputs=_parse_inputs(v.dict("inputs"), where="inputs"),
        output_format=_parse_output(v.dict("output"), where="output"),
        permissions=_parse_permissions(v.dict("permissions"), where="permissions"),
        phases=phases,
        body=body,
        raw_frontmatter=fm,
        path=path,
    )


def load_spec(path: Path) -> Spec:
    """Load a swarm spec from a markdown file. Raises SpecError on failure."""
    text = path.read_text(encoding="utf-8")
    return parse_spec(text, path=path)


def list_specs(specs_dir: Path | None = None) -> list[Spec]:
    """Load all swarm specs in a directory. Invalid specs are skipped silently
    (use load_spec directly for explicit error reporting)."""
    d = specs_dir or config.SWARM_SPECS_DIR
    if not d.is_dir():
        return []
    out: list[Spec] = []
    for p in sorted(d.glob("*.md")):
        try:
            out.append(load_spec(p))
        except SpecError:
            continue
    return out


def find_spec(name: str, specs_dir: Path | None = None) -> Spec | None:
    """Load the named spec or return None if not found. SpecError still
    raises if the file exists but is malformed — callers that want silent
    skipping should use list_specs instead."""
    d = specs_dir or config.SWARM_SPECS_DIR
    p = d / f"{name}.md"
    if not p.is_file():
        return None
    return load_spec(p)


# ---------- Input validation ----------


def validate_inputs(spec: Spec, supplied: dict) -> dict:
    """Validate user-supplied launch inputs against spec.inputs.

    Returns a fully-populated dict with defaults applied. Raises SpecError
    on missing required fields, type mismatches, range violations, or
    unknown extras. This is the single defense — fail here before any
    sub-agent spawns and any money is spent.
    """
    out: dict = {}
    by_name = {i.name: i for i in spec.inputs}
    for name, idef in by_name.items():
        if name in supplied:
            v = _coerce_to_type(supplied[name], idef.type, where=f"inputs.{name}")
            if idef.min is not None and _comparable(v) < idef.min:
                raise SpecError(f"inputs.{name} must be >= {idef.min}, got {v}")
            if idef.max is not None and _comparable(v) > idef.max:
                raise SpecError(f"inputs.{name} must be <= {idef.max}, got {v}")
            out[name] = v
        elif idef.required:
            raise SpecError(f"missing required input: {name}")
        else:
            out[name] = idef.default
    extras = set(supplied) - set(by_name)
    if extras:
        raise SpecError(f"unknown inputs: {sorted(extras)}")
    return out


def _comparable(v: Any) -> Any:
    """Length is the comparable for lists; value otherwise."""
    if isinstance(v, list):
        return len(v)
    return v


def _coerce_to_type(v: Any, type_name: str, *, where: str) -> Any:
    """Coerce a value to the named input type. Strict — bool is NOT int,
    int is NOT float (no surprising widening). The point of validation
    here is that the user gets a clean error before money is spent."""
    if type_name == "string":
        if not isinstance(v, str):
            raise SpecError(f"{where} must be string, got {type(v).__name__}")
        return v
    if type_name == "int":
        # bool is a subclass of int in Python — reject explicitly.
        if isinstance(v, bool):
            raise SpecError(f"{where} must be int, got bool")
        if not isinstance(v, int):
            raise SpecError(f"{where} must be int, got {type(v).__name__}")
        return v
    if type_name == "float":
        if isinstance(v, bool):
            raise SpecError(f"{where} must be float, got bool")
        if not isinstance(v, (int, float)):
            raise SpecError(f"{where} must be float, got {type(v).__name__}")
        return float(v)
    if type_name == "bool":
        if not isinstance(v, bool):
            raise SpecError(f"{where} must be bool, got {type(v).__name__}")
        return v
    if type_name == "list":
        if not isinstance(v, list):
            raise SpecError(f"{where} must be list, got {type(v).__name__}")
        return v
    raise SpecError(f"{where}: unknown type {type_name!r}")


# ---------- Internals: per-section parsers ----------


def _parse_budget(d: dict, *, where: str) -> Budget:
    v = Validator(d, where=where)
    return Budget(
        max_usd=float(v.optional(
            "max_usd", (int, float), default=5.0,
            min_=0, max_=config.SWARM_MAX_BUDGET_USD,
        )),
        max_wallclock_s=v.optional(
            "max_wallclock_s", int, default=600,
            min_=1, max_=config.SWARM_MAX_WALLCLOCK_S,
        ),
        max_subagents=v.optional(
            "max_subagents", int, default=10,
            min_=1, max_=config.SWARM_MAX_SUBAGENTS,
        ),
        max_recursion_depth=v.optional(
            "max_recursion_depth", int, default=2,
            min_=0, max_=config.SWARM_MAX_RECURSION_DEPTH,
        ),
        max_total_tool_calls=v.optional(
            "max_total_tool_calls", int, default=200, min_=1, max_=10000,
        ),
        max_completion_tokens_per_role=v.optional(
            "max_completion_tokens_per_role", int,
            default=config.SWARM_MAX_COMPLETION_TOKENS_PER_ROLE,
            min_=1, max_=8000,
        ),
    )


def _parse_inputs(d: dict, *, where: str) -> list[InputDef]:
    out: list[InputDef] = []
    for name, raw in d.items():
        if not isinstance(raw, dict):
            raise SpecError(
                f"{where}.{name} must be a dict, got {type(raw).__name__}"
            )
        sub = Validator(raw, where=f"{where}.{name}")
        type_ = sub.required("type", str)
        if type_ not in VALID_INPUT_TYPES:
            raise SpecError(
                f"{where}.{name}.type must be one of {VALID_INPUT_TYPES}, got {type_!r}"
            )
        out.append(InputDef(
            name=str(name),
            type=type_,
            required=sub.optional("required", bool, default=False),
            default=raw.get("default"),
            min=raw.get("min"),
            max=raw.get("max"),
        ))
    return out


def _parse_output(d: dict, *, where: str) -> str:
    v = Validator(d, where=where)
    fmt = v.optional("format", str, default="markdown")
    if fmt not in VALID_OUTPUT_FORMATS:
        raise SpecError(
            f"{where}.format must be one of {VALID_OUTPUT_FORMATS}, got {fmt!r}"
        )
    return fmt


def _parse_permissions(d: dict, *, where: str) -> Permissions:
    v = Validator(d, where=where)
    default_mode = v.optional("default_mode", str, default="plan")
    if default_mode not in VALID_PERMISSION_MODES:
        raise SpecError(
            f"{where}.default_mode must be one of {VALID_PERMISSION_MODES}, "
            f"got {default_mode!r}"
        )
    per_role = v.dict("per_role")
    for role, mode in per_role.items():
        if mode not in VALID_PERMISSION_MODES:
            raise SpecError(
                f"{where}.per_role.{role} must be one of {VALID_PERMISSION_MODES}, "
                f"got {mode!r}"
            )
    return Permissions(default_mode=default_mode, per_role=dict(per_role))


def _parse_phases(d: dict, *, where: str) -> list[Phase]:
    """Phases are a dict-of-dicts in YAML; insertion order is the run order.
    Returns a list of Phase objects in declaration order."""
    out: list[Phase] = []
    for name, raw in d.items():
        if not isinstance(raw, dict):
            raise SpecError(
                f"{where}.{name} must be a dict, got {type(raw).__name__}"
            )
        if not _NAME_RX.match(str(name)):
            raise SpecError(
                f"{where}.{name}: phase name must be kebab-case, got {name!r}"
            )
        v = Validator(raw, where=f"{where}.{name}")
        pattern = v.required("pattern", str)
        if pattern not in VALID_PATTERNS:
            raise SpecError(
                f"{where}.{name}.pattern must be one of {VALID_PATTERNS}, got {pattern!r}"
            )
        role = v.required("role", str)
        if not _NAME_RX.match(role):
            raise SpecError(
                f"{where}.{name}.role must be kebab-case, got {role!r}"
            )
        aggregator = v.required("aggregator", str)
        if aggregator not in VALID_AGGREGATORS:
            raise SpecError(
                f"{where}.{name}.aggregator must be one of {VALID_AGGREGATORS}, "
                f"got {aggregator!r}"
            )
        input_partition = v.optional("input_partition", str, default="per_item")
        if input_partition not in VALID_INPUT_PARTITIONS:
            raise SpecError(
                f"{where}.{name}.input_partition must be one of "
                f"{VALID_INPUT_PARTITIONS}, got {input_partition!r}"
            )
        out.append(Phase(
            name=str(name),
            pattern=pattern,
            role=role,
            aggregator=aggregator,
            aggregator_args=v.dict("aggregator_args"),
            model=v.optional("model", str, default=None),
            tool_names=list(v.list("tool_names")),
            capabilities=v.dict("capabilities"),
            input_partition=input_partition,
            max_per_batch=v.optional(
                "max_per_batch", int, default=5,
                min_=1, max_=config.SWARM_MAX_SUBAGENTS,
            ),
            depends_on=v.optional("depends_on", str, default=None),
        ))
    return out
