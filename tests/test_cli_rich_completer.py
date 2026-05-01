"""Tests for the slash-command completer dropdown.

Locks the Phase-21 ergonomics contract: every command in the dropdown must
carry a description and a category, customs are fed via a live provider
callable, and the back-compat `SLASH_COMMANDS` name list still mirrors the
built-in registry.
"""
from __future__ import annotations

import pytest

prompt_toolkit = pytest.importorskip("prompt_toolkit")
from prompt_toolkit.document import Document  # noqa: E402

from janus import cli_rich  # noqa: E402
from janus.commands import CustomCommand  # noqa: E402


def _complete(text: str, customs: dict | None = None):
    completer = cli_rich.SlashCompleter(lambda: customs or {})
    doc = Document(text=text, cursor_position=len(text))
    return list(completer.get_completions(doc, complete_event=None))


def test_builtin_registry_matches_legacy_name_list():
    assert cli_rich.SLASH_COMMANDS == [c.name for c in cli_rich.BUILTIN_COMMANDS]


def test_every_builtin_has_description_and_category():
    for c in cli_rich.BUILTIN_COMMANDS:
        assert c.name.startswith("/")
        assert c.description, f"{c.name} has no description"
        assert c.category == "built-in"


def test_completer_emits_descriptions_in_display_meta():
    out = _complete("/")
    assert out, "completer returned nothing for '/'"
    by_text = {c.text: c for c in out}
    assert "/help" in by_text
    # display_meta is the right-column dimmed text in prompt_toolkit's menu.
    help_meta = by_text["/help"].display_meta_text
    assert "available" in help_meta or "commands" in help_meta


def test_completer_prefix_filters_results():
    out = _complete("/co")
    names = {c.text for c in out}
    # All results start with /co
    assert names, "expected at least one /co* command"
    assert all(n.startswith("/co") for n in names)
    # Sanity: known /co* commands are present.
    assert "/cost" in names
    assert "/compact" in names
    assert "/continue" in names
    assert "/commands" in names


def test_completer_includes_custom_commands_from_provider(tmp_path):
    cc = CustomCommand(
        name="refactor",
        description="rewrite the snippet for clarity",
        body="Refactor: {args}",
        path=tmp_path / "refactor.md",
    )
    out = _complete("/", customs={"refactor": cc})
    by_text = {c.text: c for c in out}
    assert "/refactor" in by_text
    # Description appears in display_meta with a leading gutter.
    assert "rewrite the snippet for clarity" in by_text["/refactor"].display_meta_text


def test_completer_provider_is_called_lazily():
    """A future /reload should be visible without rebuilding the completer."""
    box = {"customs": {}}
    completer = cli_rich.SlashCompleter(lambda: box["customs"])

    def names_for(prefix: str) -> set[str]:
        doc = Document(text=prefix, cursor_position=len(prefix))
        return {c.text for c in completer.get_completions(doc, None)}

    assert "/foo" not in names_for("/")
    box["customs"] = {"foo": CustomCommand(
        name="foo", description="d", body="b", path=__import__("pathlib").Path("foo.md"),
    )}
    assert "/foo" in names_for("/")


def test_all_slash_commands_sorts_builtins_before_customs():
    cc = CustomCommand(name="aaa", description="x", body="y",
                       path=__import__("pathlib").Path("aaa.md"))
    merged = cli_rich._all_slash_commands({"aaa": cc})
    cats = [c.category for c in merged]
    # All built-ins come first, then all customs — no interleaving.
    first_custom = cats.index("custom")
    assert all(c == "built-in" for c in cats[:first_custom])
    assert all(c == "custom" for c in cats[first_custom:])
