from janus import memory, config


def test_parse_and_render_roundtrip():
    txt = """# user.md

## Identity
Sam — solo dev.

## Tools
PowerShell on Windows.
"""
    sections = memory.parse_sections(txt)
    assert "" in sections
    assert "Identity" in sections
    assert "Tools" in sections
    assert "Sam" in sections["Identity"]

    rendered = memory.render_sections(sections)
    again = memory.parse_sections(rendered)
    # Same sections present after round-trip.
    assert set(again.keys()) >= {"Identity", "Tools"}


def test_apply_append(janus_home):
    memory.apply([
        {"op": "create_section", "section": "Identity", "text": "Sam"},
    ])
    assert "Identity" in memory.read()
    assert "Sam" in memory.read()

    memory.apply([
        {"op": "append", "section": "Identity", "text": "solo dev"},
    ])
    body = memory.read_section("Identity")
    assert "Sam" in body
    assert "solo dev" in body


def test_apply_replace_and_delete(janus_home):
    memory.apply([
        {"op": "create_section", "section": "Tools", "text": "vim"},
        {"op": "replace", "section": "Tools", "text": "neovim"},
    ])
    assert memory.read_section("Tools") == "neovim"

    memory.apply([{"op": "delete", "section": "Tools", "text": ""}])
    assert memory.read_section("Tools") is None


def test_prepend_for_prompt_truncates(janus_home, monkeypatch):
    big = "x" * 10000
    memory.apply([{"op": "create_section", "section": "Identity", "text": big}])
    monkeypatch.setattr(config, "MEMORY_PREPEND_BYTES", 200)
    out = memory.prepend_for_prompt()
    assert "truncated for prompt" in out
    assert len(out) < 1000


def test_propose_diff_disabled(janus_home, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_PROPOSE_ENABLED", False)
    # v1.18: returns dict {"ops": [...], "cards": [...]}.
    assert memory.propose_diff("hi", "hello") == {"ops": [], "cards": []}


def test_propose_diff_parses_llm(janus_home, fake_llm):
    fake_llm.append({
        "content": '{"ops": [{"op": "create_section", "section": "Identity", "text": "Sam"}]}',
    })
    result = memory.propose_diff("im sam", "hi sam")
    ops = result["ops"]
    assert len(ops) == 1
    assert ops[0]["section"] == "Identity"
    assert ops[0]["category"] == "user"  # default when LLM omits


# ---------- v1.3 multi-category memory ----------


def test_categories_initially_empty(janus_home):
    """Fresh ~/.janus/memory/ → no categories surfaced."""
    assert memory.list_categories() == []
    assert memory.prepend_for_prompt() == ""


def test_apply_routes_to_named_category(janus_home):
    memory.apply([
        {"op": "create_section", "category": "soul",
         "section": "Name", "text": "Samoul"},
    ])
    assert memory.read("soul").strip() != ""
    assert "Samoul" in memory.read("soul")
    # Did NOT pollute user.md.
    assert memory.read("user").strip() == ""


def test_prepend_concatenates_categories_in_priority_order(janus_home):
    """soul before user before project — the order MEMORY_CATEGORIES dictates."""
    memory.apply([
        {"op": "create_section", "category": "user",
         "section": "Identity", "text": "Sam"},
        {"op": "create_section", "category": "project",
         "section": "Now", "text": "shipping v1.3"},
        {"op": "create_section", "category": "soul",
         "section": "Name", "text": "Samoul"},
    ])
    out = memory.prepend_for_prompt()
    soul_pos = out.find("soul.md")
    user_pos = out.find("user.md")
    project_pos = out.find("project.md")
    assert 0 <= soul_pos < user_pos < project_pos, (
        "expected order: soul, user, project — got positions "
        f"{soul_pos}, {user_pos}, {project_pos}"
    )


def test_extra_user_dropped_category_is_loaded(janus_home):
    """User can drop a custom category file and it appears after configured ones."""
    (config.MEMORY_DIR / "habits.md").write_text(
        "# habits.md\n\n## Morning\nCoffee then code.\n", encoding="utf-8",
    )
    cats = memory.list_categories()
    assert "habits" in cats
    out = memory.prepend_for_prompt()
    assert "habits.md" in out
    assert "Coffee then code." in out


def test_empty_category_file_not_in_prepend(janus_home):
    """An existing-but-empty .md doesn't pollute the system prompt."""
    (config.MEMORY_DIR / "soul.md").write_text("", encoding="utf-8")
    assert memory.list_categories() == []
    assert memory.prepend_for_prompt() == ""


def test_migration_moves_legacy_user_md(janus_home):
    """~/.janus/user.md → ~/.janus/memory/user.md, non-destructive."""
    config.USER_MODEL_FILE.write_text(
        "# user.md\n\n## Identity\nSam.\n", encoding="utf-8",
    )
    # Trigger via any read path.
    out = memory.read("user")
    assert "Sam." in out
    assert (config.MEMORY_DIR / "user.md").exists()
    assert not config.USER_MODEL_FILE.exists()  # moved, not copied


def test_migration_does_not_clobber_existing_new_path(janus_home):
    """Legacy user.md present AND memory/user.md present → leave both alone."""
    config.USER_MODEL_FILE.write_text(
        "legacy content", encoding="utf-8",
    )
    (config.MEMORY_DIR / "user.md").write_text(
        "# user.md\n\n## A\nnew content\n", encoding="utf-8",
    )
    # Migration should NOT move; new path wins on read.
    out = memory.read("user")
    assert "new content" in out
    assert "legacy content" not in out
    # Legacy preserved as backup.
    assert config.USER_MODEL_FILE.exists()
    assert "legacy content" in config.USER_MODEL_FILE.read_text(encoding="utf-8")


def test_propose_diff_routes_to_soul(janus_home, fake_llm):
    """LLM can propose a soul.md update — that op has category='soul'."""
    fake_llm.append({
        "content": (
            '{"ops": [{"op": "create_section", "category": "soul", '
            '"section": "Name", "text": "Samoul"}]}'
        ),
    })
    result = memory.propose_diff(
        "your name is Samoul",
        "Hi! I'm Samoul, locked and loaded.",
    )
    ops = result["ops"]
    assert len(ops) == 1
    assert ops[0]["category"] == "soul"
    memory.apply(ops)
    assert "Samoul" in memory.read("soul")
    assert memory.read("user").strip() == ""


def test_render_diff_shows_category(janus_home):
    out = memory.render_diff([
        {"op": "create_section", "category": "soul",
         "section": "Name", "text": "Samoul"},
    ])
    assert "soul.md" in out
    assert "Samoul" in out


def test_per_category_truncation(janus_home, monkeypatch):
    """Each category is independently capped at MEMORY_PREPEND_BYTES."""
    big = "x" * 10000
    memory.apply([
        {"op": "create_section", "category": "user",
         "section": "Big", "text": big},
        {"op": "create_section", "category": "soul",
         "section": "Big", "text": big},
    ])
    monkeypatch.setattr(config, "MEMORY_PREPEND_BYTES", 200)
    out = memory.prepend_for_prompt()
    # Both files surface; both truncated independently.
    assert out.count("[truncated for prompt]") == 2
