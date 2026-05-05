"""Tests for v1.18 privacy zones (Phase 4): scope_matches, current_scope,
current_project_root.

Covers the matrix of card_scope × current_scope combinations, including
project: scope hierarchical match by CWD.
"""

from __future__ import annotations
from pathlib import Path

import pytest

from janus import config, session_context


@pytest.fixture(autouse=True)
def clean_origin():
    """Each test starts with no origin set."""
    session_context.clear_origin()
    yield
    session_context.clear_origin()


# ---------- current_scope() ----------


class TestCurrentScope:
    def test_default_when_no_origin_no_project(self, tmp_path, monkeypatch):
        # WORKSPACE outside any project → cli
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        assert session_context.current_scope() == "cli"

    def test_telegram_origin(self):
        session_context.set_origin(platform="telegram", chat_id="12345")
        assert session_context.current_scope() == "telegram:12345"

    def test_web_origin(self):
        session_context.set_origin(platform="web", chat_id="sess_abc")
        assert session_context.current_scope() == "web:sess_abc"

    def test_whatsapp_origin(self):
        session_context.set_origin(platform="whatsapp", chat_id="+15551234")
        assert session_context.current_scope() == "whatsapp:+15551234"

    def test_unknown_platform_falls_through(self, tmp_path, monkeypatch):
        # An unrecognized platform shouldn't crash; falls through to project/cli
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        session_context.set_origin(platform="weird", chat_id="x")
        assert session_context.current_scope() == "cli"

    def test_project_scope_when_workspace_has_git(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        scope = session_context.current_scope()
        assert scope.startswith("project:")
        assert str(tmp_path.resolve()) in scope

    def test_project_scope_when_workspace_has_claude_md(self, tmp_path, monkeypatch):
        (tmp_path / "CLAUDE.md").write_text("# project")
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        scope = session_context.current_scope()
        assert scope.startswith("project:")

    def test_gateway_scope_overrides_project(self, tmp_path, monkeypatch):
        # Even inside a project root, telegram chat_id wins.
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        session_context.set_origin(platform="telegram", chat_id="999")
        assert session_context.current_scope() == "telegram:999"

    def test_never_returns_global(self, tmp_path, monkeypatch):
        # current_scope should never return 'global' — that requires explicit
        # user gesture in extraction (Phase 5).
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        for kwargs in [
            {},  # no origin
            {"platform": "telegram", "chat_id": "1"},
            {"platform": "web", "chat_id": "x"},
        ]:
            session_context.clear_origin()
            if kwargs:
                session_context.set_origin(**kwargs)
            assert session_context.current_scope() != "global"


# ---------- current_project_root() ----------


class TestCurrentProjectRoot:
    def test_returns_none_outside_project(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        assert session_context.current_project_root() is None

    def test_finds_git_at_workspace(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        root = session_context.current_project_root()
        assert root is not None
        assert root.resolve() == tmp_path.resolve()

    def test_finds_git_in_parent(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)
        monkeypatch.setattr(config, "WORKSPACE", sub)
        root = session_context.current_project_root()
        assert root is not None
        assert root.resolve() == tmp_path.resolve()

    def test_finds_claude_md(self, tmp_path, monkeypatch):
        (tmp_path / "CLAUDE.md").write_text("# proj")
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        root = session_context.current_project_root()
        assert root is not None

    def test_finds_janus_md(self, tmp_path, monkeypatch):
        (tmp_path / "JANUS.md").write_text("# proj")
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        root = session_context.current_project_root()
        assert root is not None

    def test_finds_agents_md(self, tmp_path, monkeypatch):
        (tmp_path / "AGENTS.md").write_text("# proj")
        monkeypatch.setattr(config, "WORKSPACE", tmp_path)
        root = session_context.current_project_root()
        assert root is not None


# ---------- scope_matches() ----------


class TestScopeMatchesGlobal:
    def test_global_matches_anything(self):
        assert session_context.scope_matches("global", "cli")
        assert session_context.scope_matches("global", "telegram:1")
        assert session_context.scope_matches("global", "project:/x")
        assert session_context.scope_matches("global", "web:s_x")


class TestScopeMatchesExact:
    def test_cli_matches_cli(self):
        assert session_context.scope_matches("cli", "cli")

    def test_telegram_exact(self):
        assert session_context.scope_matches("telegram:42", "telegram:42")

    def test_telegram_different_chats_dont_match(self):
        assert not session_context.scope_matches(
            "telegram:42", "telegram:99"
        )

    def test_web_different_sessions_dont_match(self):
        assert not session_context.scope_matches(
            "web:s_a", "web:s_b"
        )

    def test_telegram_vs_web_dont_match(self):
        assert not session_context.scope_matches(
            "telegram:42", "web:42"
        )


class TestScopeMatchesProject:
    def test_project_exact_match(self, tmp_path):
        scope = f"project:{tmp_path}"
        assert session_context.scope_matches(
            scope, scope, cwd=tmp_path,
        )

    def test_project_descendant_cwd_matches(self, tmp_path):
        sub = tmp_path / "src" / "deeper"
        sub.mkdir(parents=True)
        # Card scoped to the project root — CWD inside it should match.
        assert session_context.scope_matches(
            f"project:{tmp_path}",
            "cli",  # gateway doesn't matter; project matches by CWD
            cwd=sub,
        )

    def test_project_sibling_cwd_does_not_match(self, tmp_path):
        proj_a = tmp_path / "proj_a"
        proj_a.mkdir()
        proj_b = tmp_path / "proj_b"
        proj_b.mkdir()
        # Card scoped to proj_a; CWD in proj_b — must NOT match
        assert not session_context.scope_matches(
            f"project:{proj_a}",
            "cli",
            cwd=proj_b,
        )

    def test_project_parent_cwd_does_not_match(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        # Card scoped to sub; CWD in tmp_path (parent) — must NOT match
        assert not session_context.scope_matches(
            f"project:{sub}",
            "cli",
            cwd=tmp_path,
        )

    def test_project_match_does_not_require_current_scope_to_be_project(self, tmp_path):
        # Even when current_scope is "telegram:..." / "cli" / etc., project
        # cards still apply if CWD descends.
        assert session_context.scope_matches(
            f"project:{tmp_path}",
            "telegram:99",
            cwd=tmp_path,
        )


class TestScopeMatchesNegative:
    def test_unrelated_scopes_dont_match(self):
        assert not session_context.scope_matches("telegram:1", "cli")
        assert not session_context.scope_matches("cli", "telegram:1")
        assert not session_context.scope_matches("web:x", "cli")
