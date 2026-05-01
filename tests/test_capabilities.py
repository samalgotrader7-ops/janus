from janus.tools.capabilities import Capability, CapabilitySet, _glob_match


def test_simple_glob_no_double_star():
    assert _glob_match("git *", "git status")
    assert _glob_match("git *", "git log --oneline")
    assert not _glob_match("git *", "rm -rf /")
    # `*` does NOT cross slash
    assert not _glob_match("src/*.py", "src/sub/x.py")


def test_double_star_crosses_slashes():
    assert _glob_match("src/**", "src/a.py")
    assert _glob_match("src/**", "src/sub/dir/x.py")
    assert _glob_match("src/**/test_*.py", "src/a/b/test_x.py")
    assert not _glob_match("src/**", "lib/a.py")


def test_question_mark():
    assert _glob_match("?ello", "hello")
    assert not _glob_match("?ello", "hhello")
    # ? does NOT cross slash
    assert not _glob_match("?ello", "/ello")


def test_capability_matches():
    c = Capability("shell", "exec", ("git *", "pnpm *"))
    assert c.matches("shell", "exec", "git status")
    assert not c.matches("shell", "exec", "rm -rf /")
    assert not c.matches("fs", "write", "git status")  # tool/verb must match


def test_capability_set_from_dict():
    cs = CapabilitySet.from_dict({
        "shell.exec": ["git *", "pnpm *"],
        "fs.write": ["src/**"],
        "broken": ["should be ignored — no dot"],
    })
    assert cs.grants("shell", "exec", "git status")
    assert cs.grants("fs", "write", "src/main.py")
    assert not cs.grants("fs", "write", "lib/main.py")
    assert not cs.grants("broken", "anything", "x")


def test_capability_set_render():
    cs = CapabilitySet.from_dict({"shell.exec": ["git *"]})
    text = cs.render()
    assert "shell.exec" in text
    assert "git *" in text


def test_empty_capabilities_grant_nothing():
    cs = CapabilitySet()
    assert not cs.grants("shell", "exec", "echo hi")
