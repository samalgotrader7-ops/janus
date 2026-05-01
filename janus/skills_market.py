"""
skills_market.py — Phase 10: import skills from external sources.

Sources supported:
- Local file path to a .md skill (Anthropic / Janus format).
- Local directory containing SKILL.md and optional scripts/, references/,
  assets/ (Claude Code "directory-form skill"). Layout preserved.
- HTTP(S) URL pointing to a raw .md skill body.

INVARIANT (P4): every imported skill lands as `state: quarantined`
regardless of what the source frontmatter says. The user reads the
capabilities and decides whether to /promote.

We do NOT execute any imported scripts at import time. They live in
the skill directory; the user / agent invokes them later via the shell
tool, gated by capability.
"""

from __future__ import annotations
import datetime
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config, skills, diff as diff_mod


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
    return name or "imported-skill"


def _force_quarantine(text: str) -> tuple[str, dict]:
    """Parse, set state=quarantined, normalize counters, re-emit."""
    fm, body = skills.parse_frontmatter(text)
    if not fm:
        # No frontmatter — wrap with a minimal one.
        fm = {
            "name": "imported-skill",
            "description": "(no description)",
            "state": "quarantined",
            "created": _now_iso(),
        }
    fm["state"] = "quarantined"
    fm.pop("last-promoted", None)
    fm["last-promoted"] = None
    # Reset counters — we treat the import as a fresh slate.
    fm["runs"] = 0
    fm["success"] = 0
    fm["fail"] = 0
    fm.setdefault("created", _now_iso())
    if not fm.get("name"):
        fm["name"] = "imported-skill"
    if not fm.get("description"):
        fm["description"] = "(no description)"
    rendered = "---\n" + skills.render_frontmatter(fm) + "\n---\n\n" + body.strip() + "\n"
    return rendered, fm


# ---------- Source resolvers ----------


def _is_url(source: str) -> bool:
    try:
        u = urlparse(source)
        return u.scheme in ("http", "https")
    except Exception:
        return False


def _fetch_url(url: str) -> str:
    r = requests.get(url, timeout=config.SKILLS_MARKET_FETCH_TIMEOUT)
    r.raise_for_status()
    return r.text


# ---------- Public API ----------


def import_skill(source: str) -> Path:
    """Import a skill from `source`. Returns the path of the installed skill.

    Raises ValueError on bad input, requests.HTTPError on network failures.
    """
    config.ensure_home()

    if _is_url(source):
        text = _fetch_url(source)
        return _install_from_text(text, fallback_name=_url_to_name(source))

    p = Path(source).expanduser().resolve()
    if not p.exists():
        raise ValueError(f"source does not exist: {source}")

    if p.is_dir():
        return _install_from_directory(p)
    if p.suffix.lower() != ".md":
        raise ValueError(f"file is not a .md skill: {source}")
    return _install_from_text(
        p.read_text(encoding="utf-8"),
        fallback_name=_safe_name(p.stem),
    )


# ---------- Install paths ----------


def _install_from_text(text: str, *, fallback_name: str) -> Path:
    rendered, fm = _force_quarantine(text)
    name = _safe_name(str(fm.get("name") or fallback_name))
    target = config.SKILLS_DIR / f"{name}.md"
    target = _next_available_path(target)
    target.write_text(rendered, encoding="utf-8")
    return target


def _install_from_directory(src_dir: Path) -> Path:
    """Install a Claude Code directory-form skill.

    Layout preserved (scripts/, references/, assets/, etc. copied verbatim).
    SKILL.md frontmatter is rewritten to enforce quarantine.
    """
    skill_md = src_dir / "SKILL.md"
    if not skill_md.exists():
        skill_md = src_dir / f"{src_dir.name}.md"
    if not skill_md.exists():
        raise ValueError(
            f"directory must contain SKILL.md or {src_dir.name}.md: {src_dir}"
        )
    rendered, fm = _force_quarantine(skill_md.read_text(encoding="utf-8"))
    name = _safe_name(str(fm.get("name") or src_dir.name))
    target_dir = _next_available_path(config.SKILLS_DIR / name)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy everything except the source SKILL.md (we rewrite that).
    for entry in src_dir.iterdir():
        if entry.name in ("SKILL.md", f"{src_dir.name}.md"):
            continue
        dst = target_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dst, dirs_exist_ok=False)
        else:
            shutil.copy2(entry, dst)
    (target_dir / "SKILL.md").write_text(rendered, encoding="utf-8")
    return target_dir / "SKILL.md"


# ---------- Naming helpers ----------


def _url_to_name(url: str) -> str:
    p = urlparse(url)
    base = Path(p.path).stem or p.netloc.replace(".", "-")
    return _safe_name(base)


def diff_against_neighbor(target_md_path: Path) -> str | None:
    """When importing a skill that collides on name with one already
    installed, surface what changed. Returns a colored unified diff
    (body only) or None if no neighbor exists.

    "Neighbor" = an installed skill whose name shares the imported
    skill's stem (after stripping any -2/-3 suffix `_next_available_path`
    appended).
    """
    try:
        target = skills.load_path(target_md_path)
    except Exception:
        return None
    candidates = [
        s for s in skills.list_skills()
        if s.path.resolve() != target_md_path.resolve()
    ]
    if not candidates:
        return None
    base = re.sub(r"-\d+$", "", target.name)
    matches = [s for s in candidates if s.name == base or s.name.startswith(base + "-")]
    if not matches:
        # Fallback: prefix-of-prefix match (one-level fuzzy).
        matches = [
            s for s in candidates
            if s.name.startswith(target.name[:max(3, len(target.name) // 2)])
        ]
    if not matches:
        return None
    closest = matches[0]
    return diff_mod.render(
        closest.body, target.body,
        path=f"{closest.name} → {target.name}",
    )


def _next_available_path(p: Path) -> Path:
    """Avoid clobbering an existing skill — append -2, -3, …"""
    if not p.exists():
        return p
    base = p
    if p.suffix:
        stem, suf = p.stem, p.suffix
        i = 2
        while (p.parent / f"{stem}-{i}{suf}").exists():
            i += 1
        return p.parent / f"{stem}-{i}{suf}"
    i = 2
    while (p.parent / f"{p.name}-{i}").exists():
        i += 1
    return p.parent / f"{p.name}-{i}"
