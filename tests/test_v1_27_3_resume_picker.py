"""Tests for v1.27.3 — /resume picker upgrade (Phase 2).

Pre-v1.27.3 ``/resume`` showed a 10-row table of (id, turns, last
update). Selecting required typing the full id. v1.27.3 ships:

  * Numbered selection — ``/resume 3`` resumes the 3rd-most-recent.
  * Id prefix matching — ``/resume 2026-05-07`` resumes if there's
    one match with that prefix.
  * ``/resume search <query>`` — substring filter across title,
    first user msg, last user msg, last assistant msg, id.
  * ``/resume gateway <name>`` — filter by origin (cli_rich /
    telegram / web / ...).
  * ``/resume since <date>`` — filter by ``last_updated >= date``.
  * Picker now shows previews (title or first user msg + last
    assistant snippet), turn count, last_updated, gateway tag.
  * New ``gateway`` field on ``Conversation`` (default "" for
    legacy conversations) populated by ``conversation.new(gateway=)``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from janus import config, conversation


def _isolate_conv_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CONVERSATIONS_DIR at a fresh tmp dir for each test."""
    cdir = tmp_path / "conversations"
    cdir.mkdir()
    monkeypatch.setattr(config, "CONVERSATIONS_DIR", cdir)
    return cdir


def _save_synthetic(
    cdir: Path,
    *,
    conv_id: str,
    last_updated: str,
    title: str = "",
    gateway: str = "",
    turns: list[dict] | None = None,
) -> Path:
    """Drop a JSON conversation file into cdir for the picker to find."""
    p = cdir / f"{conv_id}.json"
    body = {
        "id": conv_id,
        "started": last_updated,
        "last_updated": last_updated,
        "model": "test-model",
        "workspace": "/tmp/ws",
        "turns": turns or [],
        "summary": "",
        "title": title,
        "pinned_turns": [],
        "gateway": gateway,
    }
    p.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return p


# ============================================================
# Conversation.gateway field
# ============================================================


def test_conversation_dataclass_has_gateway_field():
    c = conversation.Conversation(
        id="x", started="t", last_updated="t",
        model="m", workspace="/w",
    )
    assert hasattr(c, "gateway")
    assert c.gateway == ""  # default empty


def test_new_accepts_gateway_kwarg():
    c = conversation.new(model="m", workspace="/w", gateway="cli_rich")
    assert c.gateway == "cli_rich"


def test_new_default_gateway_is_empty():
    c = conversation.new(model="m", workspace="/w")
    assert c.gateway == ""


def test_save_load_round_trip_preserves_gateway(tmp_path, monkeypatch):
    _isolate_conv_dir(tmp_path, monkeypatch)
    c = conversation.new(model="m", workspace="/w", gateway="telegram")
    conversation.save(c)
    loaded = conversation.load(c.id)
    assert loaded is not None
    assert loaded.gateway == "telegram"


def test_load_legacy_conv_without_gateway(tmp_path, monkeypatch):
    """Pre-v1.27.3 conversations on disk have no `gateway` field."""
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    legacy = {
        "id": "legacy-id",
        "started": "2026-04-01T00:00:00",
        "last_updated": "2026-04-01T00:00:00",
        "model": "m",
        "workspace": "/w",
        "turns": [],
        "summary": "",
        "title": "",
        "pinned_turns": [],
        # NO gateway field — legacy
    }
    (cdir / "legacy-id.json").write_text(json.dumps(legacy), encoding="utf-8")
    loaded = conversation.load("legacy-id")
    assert loaded is not None
    assert loaded.gateway == ""  # default for missing


# ============================================================
# list_all — preview snippets + gateway
# ============================================================


def test_list_all_includes_gateway_in_summaries(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(
        cdir, conv_id="A", last_updated="2026-05-07T10:00:00",
        gateway="cli_rich",
    )
    items = conversation.list_all()
    assert len(items) == 1
    assert items[0]["gateway"] == "cli_rich"


def test_list_all_includes_first_user_msg(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(
        cdir, conv_id="A", last_updated="2026-05-07T10:00:00",
        turns=[
            {"ts": "t1", "request": "hello world", "output": "hi"},
            {"ts": "t2", "request": "second", "output": "ok"},
        ],
    )
    items = conversation.list_all()
    assert items[0]["first_user_msg"] == "hello world"


def test_list_all_includes_last_user_and_assistant_msgs(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(
        cdir, conv_id="A", last_updated="2026-05-07T10:00:00",
        turns=[
            {"ts": "t1", "request": "first req", "output": "first out"},
            {"ts": "t2", "request": "second req", "output": "second out"},
            {"ts": "t3", "request": "third req", "output": "third out"},
        ],
    )
    item = conversation.list_all()[0]
    assert item["last_user_msg"] == "third req"
    assert item["last_assistant_msg"] == "third out"


def test_list_all_truncates_long_messages(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    long_req = "x" * 500
    _save_synthetic(
        cdir, conv_id="A", last_updated="2026-05-07T10:00:00",
        turns=[{"ts": "t1", "request": long_req, "output": "y" * 500}],
    )
    item = conversation.list_all()[0]
    # Truncations: first_user_msg + last_user_msg cap at 120,
    # last_assistant_msg caps at 160
    assert len(item["first_user_msg"]) <= 120
    assert len(item["last_user_msg"]) <= 120
    assert len(item["last_assistant_msg"]) <= 160


def test_list_all_handles_empty_turns(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(
        cdir, conv_id="A", last_updated="2026-05-07T10:00:00",
        turns=[],
    )
    item = conversation.list_all()[0]
    assert item["first_user_msg"] == ""
    assert item["last_user_msg"] == ""
    assert item["last_assistant_msg"] == ""


def test_list_all_sorted_newest_first(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="old", last_updated="2026-05-01T00:00:00")
    _save_synthetic(cdir, conv_id="new", last_updated="2026-05-07T00:00:00")
    _save_synthetic(cdir, conv_id="mid", last_updated="2026-05-04T00:00:00")
    ids = [item["id"] for item in conversation.list_all()]
    assert ids == ["new", "mid", "old"]


# ============================================================
# search — query / gateway / since filters
# ============================================================


def test_search_query_matches_title(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(
        cdir, conv_id="A", last_updated="2026-05-07T10:00:00",
        title="Refactor JWT auth",
    )
    _save_synthetic(
        cdir, conv_id="B", last_updated="2026-05-07T11:00:00",
        title="Fix migration bug",
    )
    matches = conversation.search(query="jwt")
    assert len(matches) == 1
    assert matches[0]["id"] == "A"


def test_search_query_matches_first_user_msg(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(
        cdir, conv_id="A", last_updated="2026-05-07T10:00:00",
        turns=[{"ts": "t", "request": "let's add OAuth support", "output": "ok"}],
    )
    _save_synthetic(
        cdir, conv_id="B", last_updated="2026-05-07T11:00:00",
        turns=[{"ts": "t", "request": "fix typo", "output": "ok"}],
    )
    matches = conversation.search(query="oauth")
    assert len(matches) == 1
    assert matches[0]["id"] == "A"


def test_search_query_matches_id(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="2026-05-07-abc", last_updated="t")
    _save_synthetic(cdir, conv_id="2026-05-08-def", last_updated="t")
    matches = conversation.search(query="abc")
    assert len(matches) == 1


def test_search_query_case_insensitive(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(
        cdir, conv_id="A", last_updated="t", title="JWT REFACTOR",
    )
    matches = conversation.search(query="jwt refactor")
    assert len(matches) == 1


def test_search_query_empty_returns_all(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="A", last_updated="t1")
    _save_synthetic(cdir, conv_id="B", last_updated="t2")
    matches = conversation.search(query="")
    assert len(matches) == 2


def test_search_filters_by_gateway(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="A", last_updated="t1", gateway="cli_rich")
    _save_synthetic(cdir, conv_id="B", last_updated="t2", gateway="telegram")
    _save_synthetic(cdir, conv_id="C", last_updated="t3", gateway="cli_rich")
    matches = conversation.search(gateway="cli_rich")
    ids = {m["id"] for m in matches}
    assert ids == {"A", "C"}


def test_search_gateway_case_insensitive(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="A", last_updated="t1", gateway="cli_rich")
    matches = conversation.search(gateway="CLI_RICH")
    assert len(matches) == 1


def test_search_filters_by_since_date(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="old", last_updated="2026-04-01T00:00:00")
    _save_synthetic(cdir, conv_id="new", last_updated="2026-05-07T00:00:00")
    _save_synthetic(cdir, conv_id="mid", last_updated="2026-05-04T00:00:00")
    matches = conversation.search(since="2026-05-04")
    ids = {m["id"] for m in matches}
    assert ids == {"new", "mid"}


def test_search_combines_filters(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(
        cdir, conv_id="A", last_updated="2026-05-07T00:00:00",
        gateway="cli_rich", title="JWT work",
    )
    _save_synthetic(
        cdir, conv_id="B", last_updated="2026-05-07T00:00:00",
        gateway="telegram", title="JWT work too",
    )
    _save_synthetic(
        cdir, conv_id="C", last_updated="2026-05-01T00:00:00",
        gateway="cli_rich", title="JWT work old",
    )
    matches = conversation.search(
        query="jwt", gateway="cli_rich", since="2026-05-04",
    )
    ids = {m["id"] for m in matches}
    assert ids == {"A"}


def test_search_no_match_returns_empty(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="A", last_updated="t", title="x")
    assert conversation.search(query="not-a-match") == []


# ============================================================
# resolve_target — numeric / id / prefix
# ============================================================


def test_resolve_target_numeric_index_one_based(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="newest", last_updated="2026-05-07T10:00:00")
    _save_synthetic(cdir, conv_id="middle", last_updated="2026-05-07T09:00:00")
    _save_synthetic(cdir, conv_id="oldest", last_updated="2026-05-07T08:00:00")
    conv = conversation.resolve_target("1")
    assert conv is not None and conv.id == "newest"
    conv = conversation.resolve_target("2")
    assert conv is not None and conv.id == "middle"


def test_resolve_target_index_out_of_range_returns_none(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="A", last_updated="t")
    assert conversation.resolve_target("0") is None
    assert conversation.resolve_target("99") is None


def test_resolve_target_exact_id(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="2026-05-07-abc123", last_updated="t")
    conv = conversation.resolve_target("2026-05-07-abc123")
    assert conv is not None
    assert conv.id == "2026-05-07-abc123"


def test_resolve_target_unique_prefix(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="2026-05-07-aaa", last_updated="t1")
    _save_synthetic(cdir, conv_id="2026-05-08-bbb", last_updated="t2")
    conv = conversation.resolve_target("2026-05-07")
    assert conv is not None
    assert conv.id == "2026-05-07-aaa"


def test_resolve_target_ambiguous_prefix_returns_none(tmp_path, monkeypatch):
    cdir = _isolate_conv_dir(tmp_path, monkeypatch)
    _save_synthetic(cdir, conv_id="2026-05-07-aaa", last_updated="t")
    _save_synthetic(cdir, conv_id="2026-05-07-bbb", last_updated="t")
    # Prefix "2026-05-07" matches both → ambiguous
    conv = conversation.resolve_target("2026-05-07")
    assert conv is None


def test_resolve_target_no_match_returns_none(tmp_path, monkeypatch):
    _isolate_conv_dir(tmp_path, monkeypatch)
    assert conversation.resolve_target("does-not-exist") is None


def test_resolve_target_empty_string_returns_none(tmp_path, monkeypatch):
    _isolate_conv_dir(tmp_path, monkeypatch)
    assert conversation.resolve_target("") is None
    assert conversation.resolve_target("   ") is None


# ============================================================
# cli_rich /resume integration (source-pin)
# ============================================================


def test_cli_rich_resume_handler_uses_resolve_target():
    """Source-pin: the new /resume handler routes through
    conversation.resolve_target (so numeric indexes / prefixes work)."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    # The handler is in _dispatch (slash command dispatcher)
    assert "/resume" in src
    assert "resolve_target" in src


def test_cli_rich_resume_handler_supports_search_subcommand():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    # Check the /resume block specifically
    resume_idx = src.find('cmd == "/resume"')
    assert resume_idx > -1
    # Span the handler body
    body = src[resume_idx:resume_idx + 4000]
    assert "search" in body
    assert "gateway" in body
    assert "since" in body


def test_cli_rich_resume_handler_uses_search_function():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    assert "conversation.search" in src


def test_cli_rich_resume_picker_shows_preview_columns():
    """The new picker has more columns than the old (id / turns /
    last_update only)."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._dispatch)
    resume_idx = src.find('cmd == "/resume"')
    body = src[resume_idx:resume_idx + 4000]
    # Header includes a # column for numbered selection
    assert '"#"' in body or "'#'" in body
    # Title / preview column
    assert "preview" in body.lower() or "title" in body.lower()
    # Gateway column
    assert "gateway" in body.lower()


# ============================================================
# Cli_rich gateway tagging
# ============================================================


def test_cli_rich_creates_conversation_with_gateway_tag():
    """Source-pin: cli_rich's main loop calls
    conversation.new(gateway="cli_rich") so /resume gateway filter
    actually works."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert 'conversation.new(gateway="cli_rich")' in src


def test_cli_basic_creates_conversation_with_gateway_tag():
    import inspect
    from janus import cli
    src = inspect.getsource(cli)
    assert 'conversation.new(gateway="cli")' in src


def test_headless_creates_conversation_with_gateway_tag():
    import inspect
    from janus import headless
    src = inspect.getsource(headless)
    assert 'conversation.new(gateway="headless")' in src
