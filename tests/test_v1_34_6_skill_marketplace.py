"""Tests for v1.34.6 — skill marketplace catalog (Phase 7.6).

WHAT THIS SHIPS:
A central catalog index for skill discovery + install. The catalog
itself is a JSON file at JANUS_SKILLS_MARKET_URL (default:
github.com/samalgotrader7-ops/janus/main/skills_market.json).
Adding a skill = one PR adding an entry to that file.

INVARIANTS PINNED:
  * MarketEntry dataclass: name + description + url + author + tags
  * MarketEntry.matches() is case-insensitive substring across
    name / description / tags
  * fetch_index() parses JSON from URL; gracefully handles malformed
    payload (returns [] when 'skills' key missing)
  * search_index() filters by query; empty query returns all
  * install_from_market() chains to import_skill() (P4 quarantine
    preserved automatically)
  * cmd_market() dispatches list / search / info / install
  * `janus skills market` wired in __main__.py
  * skills_market.json exists at repo root with version=1 + _about
    + skills array
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from janus import skills_market


# -------------------- MarketEntry --------------------


def test_market_entry_shape():
    e = skills_market.MarketEntry(
        name="git-pr-review",
        description="Review the diff",
        url="https://example.com/skill.md",
    )
    assert e.name == "git-pr-review"
    assert e.url == "https://example.com/skill.md"
    assert e.author == ""
    assert e.tags == ()


def test_match_by_name():
    e = skills_market.MarketEntry(name="git-pr-review", description="x", url="u")
    assert e.matches("git") is True
    assert e.matches("PR") is True  # case-insensitive
    assert e.matches("xyz") is False


def test_match_by_description():
    e = skills_market.MarketEntry(name="x", description="Review the git diff", url="u")
    assert e.matches("review") is True
    assert e.matches("diff") is True


def test_match_by_tag():
    e = skills_market.MarketEntry(
        name="x", description="x", url="u", tags=("git", "review"),
    )
    assert e.matches("review") is True
    assert e.matches("Git") is True


def test_empty_query_matches_anything():
    e = skills_market.MarketEntry(name="x", description="y", url="u")
    assert e.matches("") is True


# -------------------- fetch_index --------------------


def _fake_response(body, status=200):
    """Build a minimal requests.Response stub for tests."""
    m = MagicMock()
    m.text = body if isinstance(body, str) else json.dumps(body)
    m.status_code = status
    m.raise_for_status = MagicMock()
    if status >= 400:
        import requests
        m.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return m


def test_fetch_index_parses_valid_catalog():
    payload = {
        "version": 1,
        "skills": [
            {
                "name": "git-pr-review",
                "description": "Review diffs",
                "url": "https://x/skill.md",
                "author": "@me",
                "tags": ["git", "review"],
            }
        ],
    }
    with patch.object(skills_market.requests, "get",
                      return_value=_fake_response(payload)):
        out = skills_market.fetch_index("https://catalog.example/index.json")
    assert len(out) == 1
    assert out[0].name == "git-pr-review"
    assert out[0].author == "@me"
    assert out[0].tags == ("git", "review")


def test_fetch_index_empty_catalog():
    payload = {"version": 1, "skills": []}
    with patch.object(skills_market.requests, "get",
                      return_value=_fake_response(payload)):
        out = skills_market.fetch_index("https://x/i.json")
    assert out == []


def test_fetch_index_missing_skills_key_returns_empty():
    """Missing 'skills' is not an error — an empty catalog is valid."""
    payload = {"version": 1}
    with patch.object(skills_market.requests, "get",
                      return_value=_fake_response(payload)):
        out = skills_market.fetch_index("https://x/i.json")
    assert out == []


def test_fetch_index_malformed_json_raises():
    with patch.object(skills_market.requests, "get",
                      return_value=_fake_response("not json at all")):
        with pytest.raises(ValueError):
            skills_market.fetch_index("https://x/i.json")


def test_fetch_index_skips_entries_without_name_or_url():
    payload = {"skills": [
        {"name": "ok", "url": "https://x/o.md"},
        {"description": "no name"},
        {"name": "no_url"},
        {"name": "valid2", "url": "https://x/v2.md", "author": "@you"},
    ]}
    with patch.object(skills_market.requests, "get",
                      return_value=_fake_response(payload)):
        out = skills_market.fetch_index("https://x/i.json")
    names = [e.name for e in out]
    assert names == ["ok", "valid2"]


def test_fetch_index_uses_default_url_when_none(monkeypatch):
    """When url=None, fetch_index uses _market_url() which respects
    JANUS_SKILLS_MARKET_URL or falls back to DEFAULT_MARKET_URL."""
    captured = {}

    def fake_get(url, timeout=None):
        captured["url"] = url
        return _fake_response({"skills": []})

    monkeypatch.setenv("JANUS_SKILLS_MARKET_URL", "https://override/cat.json")
    with patch.object(skills_market.requests, "get", side_effect=fake_get):
        skills_market.fetch_index()
    assert captured["url"] == "https://override/cat.json"


# -------------------- search_index --------------------


def test_search_index_empty_query_returns_all():
    es = [
        skills_market.MarketEntry(name="a", description="", url="u1"),
        skills_market.MarketEntry(name="b", description="", url="u2"),
    ]
    assert skills_market.search_index(es, "") == es


def test_search_index_filters_by_query():
    es = [
        skills_market.MarketEntry(name="git-helper", description="", url="u1"),
        skills_market.MarketEntry(name="docker-tool", description="", url="u2"),
    ]
    out = skills_market.search_index(es, "git")
    assert len(out) == 1
    assert out[0].name == "git-helper"


# -------------------- install_from_market --------------------


def test_install_from_market_unknown_name_raises():
    with patch.object(skills_market, "fetch_index", return_value=[]):
        with pytest.raises(ValueError, match="no skill named"):
            skills_market.install_from_market("nonexistent")


def test_install_from_market_chains_to_import_skill(tmp_path, monkeypatch):
    """Verifies install_from_market delegates to import_skill —
    which in turn forces P4 quarantine on whatever the source said."""
    e = skills_market.MarketEntry(
        name="git-pr",
        description="x",
        url="https://example.com/skill.md",
    )
    captured = {}

    def fake_import(source):
        captured["source"] = source
        return tmp_path / "fake.md"

    with patch.object(skills_market, "fetch_index", return_value=[e]), \
         patch.object(skills_market, "import_skill", side_effect=fake_import):
        result = skills_market.install_from_market("git-pr")
    assert captured["source"] == "https://example.com/skill.md"
    assert result == tmp_path / "fake.md"


def test_install_from_market_case_insensitive_fallback():
    """install_from_market tries exact match first, then case-insensitive."""
    e = skills_market.MarketEntry(
        name="GitPrReview", description="x", url="https://x/s.md",
    )

    def fake_import(s):
        return Path("/tmp/x.md")

    with patch.object(skills_market, "fetch_index", return_value=[e]), \
         patch.object(skills_market, "import_skill", side_effect=fake_import):
        # User typed lowercase — should still install
        result = skills_market.install_from_market("gitprreview")
    assert isinstance(result, Path)


# -------------------- CLI --------------------


def test_cmd_market_no_args_prints_usage(capsys):
    rc = skills_market.cmd_market([])
    out = capsys.readouterr().out
    assert "usage" in out.lower()
    assert rc == 2


def test_cmd_market_help_succeeds(capsys):
    rc = skills_market.cmd_market(["--help"])
    assert rc == 0


def test_cmd_market_list_empty_catalog(capsys):
    with patch.object(skills_market, "fetch_index", return_value=[]):
        rc = skills_market.cmd_market(["list"])
    out = capsys.readouterr().out
    assert "empty" in out.lower()
    assert rc == 0


def test_cmd_market_list_with_entries(capsys):
    es = [
        skills_market.MarketEntry(
            name="git-pr-review",
            description="Review the diff",
            url="https://x/s.md",
            tags=("git",),
        )
    ]
    with patch.object(skills_market, "fetch_index", return_value=es):
        rc = skills_market.cmd_market(["list"])
    out = capsys.readouterr().out
    assert "git-pr-review" in out
    assert "Review the diff" in out
    assert rc == 0


def test_cmd_market_search_no_query_errors(capsys):
    with patch.object(skills_market, "fetch_index", return_value=[]):
        rc = skills_market.cmd_market(["search"])
    assert rc == 2


def test_cmd_market_search_finds_match(capsys):
    es = [skills_market.MarketEntry(name="git-helper", description="x", url="u")]
    with patch.object(skills_market, "fetch_index", return_value=es):
        rc = skills_market.cmd_market(["search", "git"])
    out = capsys.readouterr().out
    assert "git-helper" in out
    assert rc == 0


def test_cmd_market_info_unknown_errors(capsys):
    with patch.object(skills_market, "fetch_index", return_value=[]):
        rc = skills_market.cmd_market(["info", "nope"])
    assert rc == 1


def test_cmd_market_info_prints_full_record(capsys):
    es = [skills_market.MarketEntry(
        name="git-pr",
        description="Review the diff",
        url="https://x/s.md",
        author="@me",
        tags=("git", "review"),
    )]
    with patch.object(skills_market, "fetch_index", return_value=es):
        rc = skills_market.cmd_market(["info", "git-pr"])
    out = capsys.readouterr().out
    assert "git-pr" in out
    assert "@me" in out
    assert "git, review" in out
    assert rc == 0


def test_cmd_market_install_no_name_errors(capsys):
    with patch.object(skills_market, "fetch_index", return_value=[]):
        rc = skills_market.cmd_market(["install"])
    assert rc == 2


def test_cmd_market_unknown_subcommand_errors(capsys):
    with patch.object(skills_market, "fetch_index", return_value=[]):
        rc = skills_market.cmd_market(["bogus"])
    assert rc == 2


# -------------------- skills_market.json file --------------------


def test_repo_skills_market_json_exists():
    """Repo root has a starter catalog file users can submit PRs to."""
    catalog = (
        Path(skills_market.__file__).parent.parent / "skills_market.json"
    )
    assert catalog.exists()


def test_repo_skills_market_json_valid_shape():
    catalog = (
        Path(skills_market.__file__).parent.parent / "skills_market.json"
    )
    raw = json.loads(catalog.read_text(encoding="utf-8"))
    assert "version" in raw
    assert raw["version"] == 1
    assert "skills" in raw
    assert isinstance(raw["skills"], list)


# -------------------- __main__ wiring --------------------


def test_main_dispatches_skills_market_subcommand():
    main_path = Path(skills_market.__file__).parent / "__main__.py"
    src = main_path.read_text(encoding="utf-8")
    assert 'sub == "skills"' in src
    assert 'args[1] == "market"' in src
    assert "from . import skills_market" in src


# -------------------- Version pin --------------------


def test_version_bumped_to_1_34_6_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 34, 6)
