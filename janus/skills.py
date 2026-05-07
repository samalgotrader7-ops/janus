"""
skills.py — skill format, loading, matching, promotion.

A skill is a single markdown file in ~/.janus/skills/ with YAML frontmatter
and a body. The body is the skill's prompt fragment; the frontmatter declares
identity, capabilities, and lifecycle state.

LIFECYCLE STATES:
  quarantined          → every action prompts; skill must be picked manually
  trusted-supervised   → matching capabilities auto-approve; manual pick only
  trusted-auto         → matching capabilities auto-approve; interpreter may
                         pick the skill without user confirmation

Promotion is MANUAL via /promote. We never auto-promote based on heuristics
or eval scores. The user reads the capabilities once and decides.

WHY MARKDOWN + YAML FRONTMATTER:
  - skills are prompts; the body should look like a prompt, not a config
  - the user can read, edit, fork, share, and version-control them
  - YAML frontmatter is well-known and easy to parse without a dep
"""

from __future__ import annotations
import datetime
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, llm
from .tools.capabilities import CapabilitySet


VALID_STATES = ("quarantined", "trusted-supervised", "trusted-auto")

# v1.30.1 — project_types filter. Mirrors project_detect's classification
# values plus "any" as an explicit alias for the back-compat "no field"
# behavior. Unknown values in skill frontmatter aren't fatal (skills
# still load) — they just never match a real project type, which makes
# the misconfiguration easy to notice.
KNOWN_PROJECT_TYPES = frozenset(
    {"python", "node", "rust", "go", "mixed", "unknown", "any"}
)


@dataclass
class Skill:
    name: str
    description: str
    state: str
    capabilities: CapabilitySet
    body: str
    path: Path
    raw_frontmatter: dict
    created: str = ""
    last_promoted: str | None = None
    runs: int = 0
    success: int = 0
    fail: int = 0
    # v1.30.1 — project_types filter. Empty list = "applies to any
    # project type" (back-compat with all skills predating v1.30.1).
    # Non-empty list = only auto-match in those specific project
    # types. The list can include the special value "any" which
    # behaves like an empty list.
    project_types: list[str] = field(default_factory=list)

    def grants(self, tool: str, verb: str, target: str) -> bool:
        return self.capabilities.grants(tool, verb, target)

    def matches_project_type(self, current: str | None) -> bool:
        """v1.30.1: project_types filter check.

        Returns True if this skill is allowed to match in the given
        project type. Rules:
          * No ``project_types`` field (or empty list) → match any.
          * Field present and contains "any" → match any.
          * Field present, ``current`` is empty/None → no match
            (the skill explicitly declared types; we have nothing to
            compare against, so we conservatively exclude).
          * Field present + ``current`` is in the list → match.
          * Otherwise → no match.

        This is exact string matching. Skills wanting both the pure
        type and "mixed" must declare both: ``[python, mixed]``.
        """
        if not self.project_types:
            return True
        if "any" in self.project_types:
            return True
        if not current:
            return False
        return current in self.project_types

    def evolve_capabilities_enabled(self) -> bool:
        """True if the skill opts in to capability-evolving propose-revisions.

        Body is always eligible. Capabilities are a security primitive (P2),
        so they are only revisable when the user explicitly puts
        `evolve-capabilities: true` in the skill's frontmatter.
        """
        fm = self.raw_frontmatter or {}
        return bool(
            fm.get("evolve-capabilities")
            or fm.get("evolve_capabilities")
        )

    # Phase 18: trust score derived from Phase 7 counters.

    def trust_score(self) -> float | None:
        """success / runs, or None when runs == 0 (no signal yet)."""
        if self.runs == 0:
            return None
        # Defensive: success can't exceed runs but we clamp anyway.
        return min(1.0, self.success / self.runs)

    def trust_label(self) -> str:
        """Visual label for `/skills` listings. ASCII-safe-ish."""
        score = self.trust_score()
        if score is None:
            return "—"
        if score >= 0.9:
            return "★★★"
        if score >= 0.7:
            return "★★·"
        if score >= 0.5:
            return "★··"
        return "···"


# ---------- Frontmatter parser (no PyYAML dep) ----------


_FRONTMATTER_RX = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Tiny YAML-ish parser. Supports:
      key: value
      key:
        - item
        - item
      key:
        nested.key:
          - item
    Returns (frontmatter_dict, body).
    """
    m = _FRONTMATTER_RX.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    fm = _parse_yaml_subset(fm_text)
    return fm, body.lstrip("\n")


def _parse_yaml_subset(text: str) -> dict:
    """Hand-rolled subset of YAML — enough for skill frontmatter."""
    lines = text.splitlines()
    out: dict = {}
    stack: list[tuple[int, dict | list, str | None]] = [(0, out, None)]

    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()

        # Pop stack to current indent.
        while stack and indent < stack[-1][0]:
            stack.pop()

        # List item?
        if line.startswith("- "):
            value = _coerce_scalar(line[2:].strip())
            container = stack[-1][1]
            if isinstance(container, list):
                container.append(value)
            elif isinstance(container, dict):
                key = stack[-1][2]
                if key is None:
                    raise ValueError(f"list item at top of dict: {line}")
                lst = container.setdefault(key, [])
                if not isinstance(lst, list):
                    lst = []
                    container[key] = lst
                lst.append(value)
            i += 1
            continue

        # key: value or key:
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            container = stack[-1][1]
            if not isinstance(container, dict):
                # entering a dict from list context
                container = {}
                lst = stack[-1][1]
                if isinstance(lst, list):
                    lst.append(container)
                stack[-1] = (stack[-1][0], container, None)
            if v == "":
                # Look ahead: next non-blank line decides list-of vs nested dict.
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and lines[j].lstrip().startswith("- "):
                    container[k] = []
                    stack.append((indent + 2, container[k], None))
                else:
                    container[k] = {}
                    stack.append((indent + 2, container[k], None))
            else:
                container[k] = _coerce_scalar(v)
            i += 1
            continue

        i += 1

    return out


def _coerce_scalar(s: str):
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    if s.lower() in ("null", "none", "~"):
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def render_frontmatter(d: dict) -> str:
    """Inverse of _parse_yaml_subset, strict-enough for our schema."""
    lines: list[str] = []
    for k, v in d.items():
        _emit(lines, k, v, indent=0)
    return "\n".join(lines)


def _emit(lines: list[str], key: str, value, indent: int) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        lines.append(f"{pad}{key}:")
        for k, v in value.items():
            _emit(lines, k, v, indent + 1)
    elif isinstance(value, list):
        lines.append(f"{pad}{key}:")
        for item in value:
            if isinstance(item, str):
                lines.append(f'{pad}  - "{item}"')
            else:
                lines.append(f"{pad}  - {item}")
    elif value is None:
        lines.append(f"{pad}{key}: null")
    elif isinstance(value, bool):
        lines.append(f"{pad}{key}: {'true' if value else 'false'}")
    else:
        lines.append(f"{pad}{key}: {value}")


# ---------- Load + list ----------


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def list_skills() -> list[Skill]:
    config.ensure_home()
    out: list[Skill] = []
    for p in sorted(config.SKILLS_DIR.glob("*.md")):
        try:
            out.append(load_path(p))
        except Exception:
            continue
    return out


def load(name: str) -> Skill | None:
    p = config.SKILLS_DIR / f"{name}.md"
    if not p.exists():
        return None
    return load_path(p)


def load_path(p: Path) -> Skill:
    text = p.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    state = str(fm.get("state", "quarantined")).strip()
    if state not in VALID_STATES:
        state = "quarantined"
    caps = CapabilitySet.from_dict(fm.get("capabilities") or {})
    # v1.30.1 — accept project_types as a list or comma-separated
    # string. Hand-rolled YAML subset emits lists for `- item` blocks;
    # tolerate scalars in case a user writes `project_types: python`.
    raw_pt = fm.get("project_types") or fm.get("project-types") or []
    if isinstance(raw_pt, str):
        project_types = [
            t.strip().lower() for t in raw_pt.split(",") if t.strip()
        ]
    elif isinstance(raw_pt, list):
        project_types = [str(t).strip().lower() for t in raw_pt if str(t).strip()]
    else:
        project_types = []
    return Skill(
        name=str(fm.get("name") or p.stem),
        description=str(fm.get("description", "")),
        state=state,
        capabilities=caps,
        body=body.strip(),
        path=p,
        raw_frontmatter=fm,
        created=str(fm.get("created", "")),
        last_promoted=fm.get("last-promoted") or fm.get("last_promoted"),
        runs=int(fm.get("runs") or 0),
        success=int(fm.get("success") or 0),
        fail=int(fm.get("fail") or 0),
        project_types=project_types,
    )


def save(skill: Skill) -> None:
    """Persist a skill atomically (write tmp + os.replace).

    Atomic write protects the counters against half-written files on crash;
    record_run() relies on this guarantee to satisfy the Phase 7 acceptance
    criterion ("never lost on crash").
    """
    config.ensure_home()
    fm = dict(skill.raw_frontmatter or {})
    fm["name"] = skill.name
    fm["description"] = skill.description
    fm["state"] = skill.state
    fm["capabilities"] = _caps_to_dict(skill.capabilities)
    fm.setdefault("created", skill.created or _now())
    fm["last-promoted"] = skill.last_promoted
    fm["runs"] = skill.runs
    fm["success"] = skill.success
    fm["fail"] = skill.fail
    # v1.30.1 — persist project_types only when set (an empty list
    # is the back-compat default; writing it explicitly would clutter
    # every existing skill on the next save).
    if skill.project_types:
        fm["project_types"] = list(skill.project_types)
    else:
        fm.pop("project_types", None)
        fm.pop("project-types", None)

    text = "---\n" + render_frontmatter(fm) + "\n---\n\n" + skill.body.strip() + "\n"
    _atomic_write(skill.path, text)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_run(name: str, *, success: bool | None = None) -> Skill | None:
    """Increment a skill's runs counter (and success/fail when known).

    `success=True`  → bump runs and success.
    `success=False` → bump runs and fail.
    `success=None`  → bump runs only (no signal available).

    Returns the updated Skill, or None if the skill no longer exists.
    Callers MUST treat None as "skip silently"; per P8, we do not raise
    into the executor loop because of a missing skill file.
    """
    skill = load(name)
    if skill is None:
        return None
    skill.runs += 1
    if success is True:
        skill.success += 1
    elif success is False:
        skill.fail += 1
    save(skill)
    return skill


def _caps_to_dict(caps: CapabilitySet) -> dict:
    out: dict[str, list[str]] = {}
    for c in caps.caps:
        out[f"{c.tool}.{c.verb}"] = list(c.globs)
    return out


# ---------- Promotion ----------


class PromotionError(ValueError):
    pass


def promote(name: str, new_state: str) -> Skill:
    if new_state not in VALID_STATES:
        raise PromotionError(
            f"unknown state '{new_state}'. valid: {', '.join(VALID_STATES)}"
        )
    skill = load(name)
    if skill is None:
        raise PromotionError(f"no skill named '{name}'")
    skill.state = new_state
    skill.last_promoted = _now()
    save(skill)
    return skill


# ---------- Matching ----------


_WORD_RX = re.compile(r"[a-z0-9]+")


def match(
    request: str,
    skills: list[Skill] | None = None,
    *,
    project_type: str | None = None,
) -> list[Skill]:
    """Return skills whose description shares words with the request, ranked.

    Cheap lexical match — Jaccard over normalized word sets. We only need
    to surface "is there a skill that probably applies?". The interpreter
    + user make the final decision.

    v1.30.1 — ``project_type`` filter. When a skill declares
    ``project_types`` in its frontmatter, only match it if the current
    project's detected type is in that list. Skills without the field
    match any project (back-compat). When ``project_type`` is None,
    auto-detect via ``project_detect.detect_project_type``; pass an
    explicit value (including ``""``) to suppress detection — useful
    in tests and in headless contexts where the workspace is irrelevant.
    """
    skills = skills if skills is not None else list_skills()
    if not skills:
        return []
    if project_type is None:
        try:
            from . import project_detect as _pd
            project_type = _pd.detect_project_type().type
        except Exception:
            project_type = ""
    skills = [s for s in skills if s.matches_project_type(project_type)]
    if not skills:
        return []
    req_words = set(_WORD_RX.findall(request.lower()))
    if not req_words:
        return []
    scored: list[tuple[float, Skill]] = []
    for s in skills:
        desc_words = set(_WORD_RX.findall((s.description + " " + s.name).lower()))
        if not desc_words:
            continue
        overlap = len(req_words & desc_words)
        if overlap == 0:
            continue
        score = overlap / max(1, len(req_words | desc_words))
        scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored]


# ---------- Skill creation via LLM ----------


DRAFT_SYSTEM = """You draft a janus skill from recent log entries plus a
user-provided pattern description.

A skill is a markdown file with YAML frontmatter and a body. The body is the
prompt fragment that gets injected when the skill is active.

Your job:
  1. Identify the recurring task pattern in the log + user description.
  2. Pick a kebab-case name (e.g., "git-pr", "py-refactor", "data-explore").
  3. Write a one-line description that would help future-you decide whether
     to invoke this skill.
  4. Infer the MINIMAL capability set from the tools the log used. Be tight:
     prefer specific globs ("git status", "git log *") over broad ones ("git *").
  5. Write a body: a short numbered procedure the agent should follow.

Return STRICT JSON with this shape:
{
  "name": "kebab-case",
  "description": "one line",
  "capabilities": {"shell.exec": ["..."], "fs.read": ["..."]},
  "body": "markdown procedure"
}

No prose, no fences, no commentary."""


def draft_skill_from_log(pattern: str, log_records: list[dict]) -> dict:
    """Ask the LLM to draft a skill. Returns the parsed JSON dict."""
    excerpt = []
    for r in log_records[-20:]:
        tools = []
        for s in r.get("trace", []) or []:
            if s.get("type") == "tool_call":
                tools.append(s.get("tool"))
        excerpt.append({
            "request": r.get("request", "")[:300],
            "tools": tools,
            "output_head": (r.get("output") or "")[:300],
            "feedback": r.get("feedback"),
        })
    user_msg = (
        f"Pattern the user wants to capture:\n{pattern}\n\n"
        f"Last log entries (compact):\n{excerpt}"
    )
    msg = llm.chat(
        messages=[
            {"role": "system", "content": DRAFT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        json_mode=True,
        temperature=0.4,
    )
    return llm.parse_json_loose(msg.get("content") or "{}")


def write_draft(draft: dict) -> Path:
    """Persist a draft dict as a quarantined skill file."""
    name = str(draft.get("name") or "untitled-skill").strip()
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "untitled-skill"
    desc = str(draft.get("description") or "").strip()
    body = str(draft.get("body") or "(no body)").strip()
    caps = draft.get("capabilities") or {}

    config.ensure_home()
    p = config.SKILLS_DIR / f"{name}.md"
    if p.exists():
        # Don't overwrite — append a numeric suffix.
        i = 2
        while (config.SKILLS_DIR / f"{name}-{i}.md").exists():
            i += 1
        p = config.SKILLS_DIR / f"{name}-{i}.md"

    skill = Skill(
        name=p.stem,
        description=desc,
        state="quarantined",
        capabilities=CapabilitySet.from_dict(caps),
        body=body,
        path=p,
        raw_frontmatter={},
        created=_now(),
        last_promoted=None,
        runs=0,
    )
    save(skill)
    return p
