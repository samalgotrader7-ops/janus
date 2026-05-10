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


# v1.34.6 — Phase 7.6: skill marketplace (real, not just URL import).
# Default catalog URL; users can override with JANUS_SKILLS_MARKET_URL.
DEFAULT_MARKET_URL = (
    "https://raw.githubusercontent.com/samalgotrader7-ops/janus/main/skills_market.json"
)


def _market_url() -> str:
    import os
    return os.environ.get("JANUS_SKILLS_MARKET_URL") or DEFAULT_MARKET_URL


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


# ---------- v1.34.6 — Phase 7.6: catalog browse + install ----------
#
# DESIGN:
# A catalog index is a JSON document at JANUS_SKILLS_MARKET_URL
# (default: github.com/samalgotrader7-ops/janus/main/
# skills_market.json). The schema is intentionally tiny — adding an
# entry is a one-line PR to skills_market.json, no infrastructure.
#
# Index shape:
#   {
#     "version": 1,
#     "skills": [
#       {
#         "name": "git-pr-review",
#         "description": "Review the diff of the current branch.",
#         "url": "https://raw.githubusercontent.com/.../skill.md",
#         "author": "@user",
#         "tags": ["git", "review"]
#       },
#       ...
#     ]
#   }
#
# Privacy note: fetch_index() is the only network call. It downloads
# JSON, never executes anything. install() chains to the existing
# import_skill() which forces quarantine — user reviews before
# /promote. P4 invariant preserved.


import json as _json
from dataclasses import dataclass, asdict


@dataclass
class MarketEntry:
    """One skill listed in the marketplace catalog."""

    name: str
    description: str
    url: str
    author: str = ""
    tags: tuple[str, ...] = ()

    def matches(self, query: str) -> bool:
        """Case-insensitive substring search across name, description, tags."""
        if not query:
            return True
        q = query.lower()
        if q in self.name.lower() or q in self.description.lower():
            return True
        return any(q in t.lower() for t in self.tags)


def fetch_index(url: str | None = None, *, timeout: int | None = None) -> list[MarketEntry]:
    """Download + parse the catalog. Returns the list of entries.

    Network failures raise requests.HTTPError; malformed JSON raises
    ValueError; missing 'skills' key returns an empty list (treated
    as 'catalog is empty', not an error)."""
    chosen = url or _market_url()
    t = timeout if timeout is not None else config.SKILLS_MARKET_FETCH_TIMEOUT
    resp = requests.get(chosen, timeout=t)
    resp.raise_for_status()
    try:
        raw = _json.loads(resp.text)
    except _json.JSONDecodeError as e:
        raise ValueError(f"catalog at {chosen} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        return []
    skills_arr = raw.get("skills")
    if not isinstance(skills_arr, list):
        return []
    out: list[MarketEntry] = []
    for s in skills_arr:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        url_v = str(s.get("url") or "").strip()
        if not name or not url_v:
            continue
        tags_raw = s.get("tags") or []
        if not isinstance(tags_raw, list):
            tags_raw = []
        out.append(MarketEntry(
            name=name,
            description=str(s.get("description") or "").strip(),
            url=url_v,
            author=str(s.get("author") or "").strip(),
            tags=tuple(str(t).strip() for t in tags_raw if str(t).strip()),
        ))
    return out


def search_index(
    entries: list[MarketEntry], query: str,
) -> list[MarketEntry]:
    """Filter entries by case-insensitive substring match against
    name / description / tags. Empty query returns all entries."""
    return [e for e in entries if e.matches(query)]


def install_from_market(name: str, *, url: str | None = None) -> Path:
    """Look up `name` in the catalog and install via existing
    import_skill() pipeline. Returns the installed skill path.

    Raises ValueError when name doesn't match any entry (case-
    sensitive on the canonical entry name)."""
    entries = fetch_index(url=url)
    match = next((e for e in entries if e.name == name), None)
    if match is None:
        # Try case-insensitive fallback to be friendlier to humans
        match = next(
            (e for e in entries if e.name.lower() == name.lower()),
            None,
        )
    if match is None:
        raise ValueError(
            f"no skill named {name!r} in the marketplace catalog. "
            f"Browse with `janus skills market list`."
        )
    return import_skill(match.url)


# ---------- CLI dispatch ----------


def cmd_market(args: list[str]) -> int:
    """`janus skills market {list|search <q>|install <name>|info <name>}`"""
    import sys
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(
            "usage: janus skills market {list | search <q> | "
            "install <name> | info <name>}\n"
            "  list                  show all skills in the catalog\n"
            "  search <query>        substring match in name/desc/tags\n"
            "  install <name>        download + install (lands quarantined)\n"
            "  info <name>           show one entry's full record\n"
        )
        return 0 if args else 2

    sub = args[0]
    rest = args[1:]

    try:
        entries = fetch_index()
    except requests.HTTPError as e:
        sys.stderr.write(f"error: catalog fetch failed: {e}\n")
        return 1
    except (ValueError, requests.RequestException) as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    if sub == "list":
        if not entries:
            sys.stdout.write("(catalog is empty)\n")
            return 0
        for e in entries:
            tag_str = f"  [{', '.join(e.tags)}]" if e.tags else ""
            sys.stdout.write(f"  {e.name:30s}  {e.description[:60]}{tag_str}\n")
        sys.stdout.write(f"\n{len(entries)} skill(s) in catalog.\n")
        return 0

    if sub == "search":
        if not rest:
            sys.stderr.write("usage: janus skills market search <query>\n")
            return 2
        query = " ".join(rest)
        hits = search_index(entries, query)
        if not hits:
            sys.stdout.write(f"(no matches for {query!r})\n")
            return 0
        for e in hits:
            sys.stdout.write(f"  {e.name:30s}  {e.description[:60]}\n")
        sys.stdout.write(f"\n{len(hits)} match(es).\n")
        return 0

    if sub == "info":
        if not rest:
            sys.stderr.write("usage: janus skills market info <name>\n")
            return 2
        target = rest[0]
        match = next((e for e in entries if e.name == target), None)
        if match is None:
            sys.stderr.write(f"error: no skill named {target!r}\n")
            return 1
        sys.stdout.write(f"name:        {match.name}\n")
        sys.stdout.write(f"description: {match.description}\n")
        sys.stdout.write(f"url:         {match.url}\n")
        if match.author:
            sys.stdout.write(f"author:      {match.author}\n")
        if match.tags:
            sys.stdout.write(f"tags:        {', '.join(match.tags)}\n")
        return 0

    if sub == "install":
        if not rest:
            sys.stderr.write("usage: janus skills market install <name>\n")
            return 2
        target = rest[0]
        try:
            path = install_from_market(target)
        except ValueError as e:
            sys.stderr.write(f"error: {e}\n")
            return 1
        except Exception as e:
            sys.stderr.write(f"error: install failed: {type(e).__name__}: {e}\n")
            return 1
        sys.stdout.write(
            f"installed (quarantined) at {path}\n"
            f"review the body and `/promote {target}` when ready.\n"
        )
        return 0

    sys.stderr.write(f"error: unknown subcommand {sub!r}\n")
    sys.stderr.write("usage: janus skills market {list|search|info|install}\n")
    return 2


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
