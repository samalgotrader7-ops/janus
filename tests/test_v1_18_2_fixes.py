"""Regression tests for v1.18.2 — latency + friendliness fixes.

Three issues from Sam's 2026-05-06 8:00 AM Telegram session:

1. Multi-minute delay because workspace boundary blocked fs_read of
   ~/.janus/memory/user.md → fallback to `shell cat` → per-call approval
   prompts → user clicks took 3+ min each.

2. Tool spam: memory_search called 8 times in a row (once per type)
   when one call would have answered the question.

3. Bot felt robotic; user wanted reactions to messages.

Fixes:
- fs_read / fs_list accept paths under ~/.janus/ (read-only carve-out).
- System prompt rules 18 + 19 tell the model not to fs_read memory
  (it's already injected) and to call memory_search ONCE per query.
- New telegram_react tool + Telegram-specific friendliness prompt.
"""

from __future__ import annotations
from pathlib import Path

import pytest

from janus import config
from janus.tools.fs import FsRead, FsList, FsWrite
from janus.tools.telegram_react import TelegramReact


def _approve(*args, **kwargs):
    return True


# ---------- fs read carve-out for ~/.janus/ ----------


@pytest.fixture
def carved_home(tmp_path, monkeypatch):
    """Set WORKSPACE and HOME to two DIFFERENT directories so we can
    distinguish workspace-vs-home access."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "agent_home"
    home.mkdir()
    (home / "memory").mkdir()
    (home / "memory" / "user.md").write_text(
        "## Identity\n\nSam, network engineer.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    return {"workspace": workspace, "home": home}


class TestFsReadCarveout:
    def test_workspace_path_still_works(self, carved_home):
        """Regression: existing workspace paths must keep working."""
        ws = carved_home["workspace"]
        (ws / "in_workspace.txt").write_text("workspace content")
        out = FsRead().run({"path": "in_workspace.txt"}, _approve)
        assert "workspace content" in out

    def test_path_outside_both_still_rejected(self, carved_home, tmp_path):
        """Random outside path → still refused."""
        outside = tmp_path / "outside.txt"
        outside.write_text("attacker content")
        out = FsRead().run({"path": str(outside)}, _approve)
        assert "error" in out.lower()
        assert "outside" in out.lower()

    def test_absolute_path_under_home_works(self, carved_home):
        """Pre-fix bug: model passed absolute path to ~/.janus/memory/user.md
        → blocked by workspace check → 6-min shell-fallback approval delay."""
        home_path = carved_home["home"] / "memory" / "user.md"
        out = FsRead().run({"path": str(home_path)}, _approve)
        assert "Sam, network engineer" in out

    def test_tilde_prefixed_path_resolves_via_HOME_env(
        self, carved_home, monkeypatch,
    ):
        """When the model writes ``~/...`` paths, expanduser uses $HOME.
        We point $HOME at the carved agent_home so the expansion lands
        in a place the carve-out accepts."""
        # Set HOME (and USERPROFILE for Windows compatibility) so
        # Path.expanduser() resolves into our carved tree.
        monkeypatch.setenv("HOME", str(carved_home["home"].parent))
        monkeypatch.setenv("USERPROFILE", str(carved_home["home"].parent))
        # config.HOME is what the carve-out compares against
        monkeypatch.setattr(config, "HOME", carved_home["home"].parent)
        out = FsRead().run(
            {"path": "~/agent_home/memory/user.md"}, _approve,
        )
        assert "Sam" in out

    def test_fs_list_carve_out(self, carved_home):
        """fs_list of ~/.janus/memory should work too."""
        out = FsList().run(
            {"path": str(carved_home["home"] / "memory")}, _approve,
        )
        # The user.md file we created is listed
        assert "user.md" in out

    def test_random_outside_rejected(self, carved_home, tmp_path):
        """Defense in depth — paths outside both workspace AND home stay
        rejected. Carve-out doesn't open up the whole filesystem."""
        random_outside = tmp_path / "elsewhere"
        random_outside.mkdir()
        out = FsList().run({"path": str(random_outside)}, _approve)
        assert "error" in out.lower()


class TestFsWriteUnchanged:
    """fs_write keeps STRICT workspace boundary — no carve-out for writes.
    Defense in depth: read access to ~/.janus/ is harmless; writes to
    arbitrary places are not."""

    def test_fs_write_to_home_still_refused(self, carved_home):
        """Writing to ~/.janus/memory/anything must still fail (the
        carve-out is read-only — defense in depth)."""
        target = str(carved_home["home"] / "memory" / "new.md")
        # FsWrite uses _resolve_within_workspace which raises ValueError
        # on escape — this is the safe boundary behavior we want to
        # preserve. The Registry catches the exception in production;
        # here we just assert it raises.
        with pytest.raises(ValueError, match="outside workspace"):
            FsWrite().run(
                {"path": target, "content": "new content"},
                _approve,
            )

    def test_fs_write_in_workspace_works(self, carved_home):
        ws_target = "in_workspace.txt"
        out = FsWrite().run(
            {"path": ws_target, "content": "ok"}, _approve,
        )
        assert "wrote" in out.lower()


# ---------- System prompt rules 18 + 19 ----------


class TestSystemPromptRules:
    def test_rule_18_says_memory_already_injected(self):
        from janus.executor import JANUS_CHAT_SYSTEM
        # Rule 18: "Memory is INJECTED at the top of this prompt"
        assert "Memory is INJECTED" in JANUS_CHAT_SYSTEM
        assert "do NOT need to" in JANUS_CHAT_SYSTEM
        # Reference to the actual anti-pattern
        assert "fs_read user.md" in JANUS_CHAT_SYSTEM

    def test_rule_18_warns_against_shell_cat_memory(self):
        from janus.executor import JANUS_CHAT_SYSTEM
        # The exact failure mode Sam hit
        assert "shell cat" in JANUS_CHAT_SYSTEM

    def test_rule_19_says_memory_search_is_one_shot(self):
        from janus.executor import JANUS_CHAT_SYSTEM
        # Rule 19: "memory_search is MULTI-TYPE by default. Call it ONCE"
        assert "memory_search" in JANUS_CHAT_SYSTEM
        assert "ONCE" in JANUS_CHAT_SYSTEM
        # Reference to the bug
        assert "Eight calls" in JANUS_CHAT_SYSTEM


# ---------- TelegramReact tool ----------


class TestTelegramReactSurface:
    def test_metadata(self):
        t = TelegramReact()
        assert t.name == "telegram_react"
        assert t.risk == "read"
        assert t.dangerous is False
        assert "emoji" in t.parameters["properties"]
        assert t.parameters["required"] == ["emoji"]

    def test_no_callback_returns_friendly_error(self):
        """Outside the Telegram gateway, the tool must NOT crash —
        return an observation the model can act on."""
        t = TelegramReact()  # no callback
        out = t.run({"emoji": "👍"}, _approve)
        assert "not in a Telegram" in out or "not on Telegram" in out

    def test_empty_emoji_rejected(self):
        t = TelegramReact()
        out = t.run({"emoji": ""}, _approve)
        assert "error" in out.lower()
        assert "emoji" in out.lower()

    def test_callback_returns_none_means_no_recent_message(self):
        """Closure returns None → no recent inbound — graceful skip."""
        t = TelegramReact(msg_id_callback=lambda: None)
        out = t.run({"emoji": "👍"}, _approve)
        assert "no recent inbound message" in out

    def test_callback_supplies_chat_and_message_id(self, monkeypatch):
        """Happy path: callback returns (chat_id, message_id), tool calls
        Telegram API. We mock requests.post to assert payload shape."""
        captured = {}

        class FakeResp:
            status_code = 200
            text = "{}"

        def fake_post(url, *, json=None, timeout=10):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

        from janus.tools import telegram_react as tr_mod
        monkeypatch.setattr(tr_mod, "requests", type("R", (), {
            "post": staticmethod(fake_post),
            "RequestException": Exception,
        }))
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "TESTTOKEN:ABC")

        t = TelegramReact(msg_id_callback=lambda: (42, 999))
        out = t.run({"emoji": "🔥"}, _approve)
        assert "reacted" in out.lower()
        # Payload shape per Telegram Bot API setMessageReaction
        assert captured["json"]["chat_id"] == "42"
        assert captured["json"]["message_id"] == 999
        assert captured["json"]["reaction"] == [
            {"type": "emoji", "emoji": "🔥"},
        ]
        assert "setMessageReaction" in captured["url"]

    def test_no_token_returns_error(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        t = TelegramReact(msg_id_callback=lambda: (42, 999))
        out = t.run({"emoji": "👍"}, _approve)
        assert "error" in out.lower()
        assert "token" in out.lower()


# ---------- TelegramReact registration ----------


class TestRegistration:
    def test_telegram_react_in_default_registry(self):
        """Outside the Telegram gateway, the bundled instance is
        callback-less and gracefully no-ops — but it IS in the registry
        so the model knows it exists."""
        from janus.tools import default_registry
        reg = default_registry()
        names = [s["function"]["name"] for s in reg.schemas()]
        assert "telegram_react" in names


# ---------- Telegram friendliness preamble ----------


class TestTelegramFriendliness:
    def test_friendliness_prompt_exists(self):
        from janus.gateways import telegram as tg
        assert hasattr(tg, "TELEGRAM_FRIENDLINESS_PROMPT")
        prompt = tg.TELEGRAM_FRIENDLINESS_PROMPT
        # Mentions the react tool
        assert "telegram_react" in prompt
        # Sets the tone shift
        assert "Telegram" in prompt
        # Tells model not to narrate tool calls
        assert "narrate" in prompt.lower() or "narrating" in prompt.lower()
