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
    assert memory.propose_diff("hi", "hello") == []


def test_propose_diff_parses_llm(janus_home, fake_llm):
    fake_llm.append({
        "content": '{"ops": [{"op": "create_section", "section": "Identity", "text": "Sam"}]}',
    })
    ops = memory.propose_diff("im sam", "hi sam")
    assert len(ops) == 1
    assert ops[0]["section"] == "Identity"
