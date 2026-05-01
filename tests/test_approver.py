from janus.tools.base import make_capability_aware
from janus.tools.capabilities import CapabilitySet


def test_capability_grant_skips_user_prompt():
    calls = []

    def base(action, details):
        calls.append((action, details))
        return False  # would deny

    caps = CapabilitySet.from_dict({"shell.exec": ["git *"]})
    wrapped = make_capability_aware(base, caps)

    ok = wrapped("shell exec", "details", capability=("shell", "exec", "git status"))
    assert ok is True
    assert calls == []


def test_no_capability_falls_through():
    calls = []

    def base(action, details):
        calls.append((action, details))
        return True

    caps = CapabilitySet.from_dict({"shell.exec": ["git *"]})
    wrapped = make_capability_aware(base, caps)

    ok = wrapped("shell exec", "details", capability=("shell", "exec", "rm -rf /"))
    assert ok is True
    assert calls == [("shell exec", "details")]


def test_no_capability_kwarg_falls_through():
    calls = []

    def base(action, details):
        calls.append((action, details))
        return True

    caps = CapabilitySet.from_dict({"shell.exec": ["git *"]})
    wrapped = make_capability_aware(base, caps)

    ok = wrapped("shell exec", "details")
    assert ok is True
    assert calls == [("shell exec", "details")]


def test_empty_caps_always_falls_through():
    calls = []

    def base(action, details):
        calls.append((action, details))
        return False

    wrapped = make_capability_aware(base, CapabilitySet())
    ok = wrapped("shell exec", "details", capability=("shell", "exec", "git status"))
    assert ok is False
    assert len(calls) == 1
