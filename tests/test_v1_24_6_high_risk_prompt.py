"""Tests for v1.24.6 #5 — high-risk approval prompts suppress
[s]ession/[a]lways grant options.

Sam's 2026-05-07 7:26 AM session: the approval prompt for
``fs_write docs/SWARM_EXPLAINER.md`` showed
``[Y]es  [s]ession  [a]lways  [N]o``. One fat-finger of `a` and
Janus would have written to docs/ forever — the persistent grant is
keyed on (tool_name, risk), so a `[a]lways approve fs_write` would
auto-green-light writes anywhere, not just docs/.

Fix: detect high-risk shapes (protected-path writes, regret-pattern
shell commands) at prompt time and offer only ``[Y]es  [N]o``.
A user who genuinely wants to approve repeatedly can do so per-call.
"""
from __future__ import annotations


# ---------- _is_high_risk_grant detector ----------


def test_fs_write_to_docs_is_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, reason = _is_high_risk_grant(
        "fs_write", ("fs", "write", "docs/SWARM_EXPLAINER.md"),
    )
    assert is_hr is True
    assert "docs" in reason.lower() or "protected" in reason.lower()


def test_fs_write_to_dot_github_is_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "fs_write", ("fs", "write", ".github/workflows/ci.yml"),
    )
    assert is_hr is True


def test_fs_write_to_vendor_is_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "fs_write", ("fs", "write", "vendor/lib.js"),
    )
    assert is_hr is True


def test_fs_write_to_license_is_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "fs_write", ("fs", "write", "LICENSE"),
    )
    assert is_hr is True


def test_fs_write_to_normal_path_is_not_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "fs_write", ("fs", "write", "src/foo.py"),
    )
    assert is_hr is False


def test_fs_edit_on_protected_path_is_high_risk():
    """fs_edit gets the same treatment — model can't bypass via edit."""
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "fs_edit", ("fs", "write", "docs/ARCHITECTURE.md"),
    )
    assert is_hr is True


def test_shell_force_push_is_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, reason = _is_high_risk_grant(
        "shell", ("shell", "exec", "git push --force origin main"),
    )
    assert is_hr is True
    assert "regret" in reason.lower() or "force" in reason.lower()


def test_shell_terraform_destroy_is_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "shell", ("shell", "exec", "terraform destroy -auto-approve"),
    )
    assert is_hr is True


def test_shell_kubectl_delete_is_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "shell", ("shell", "exec", "kubectl delete deployment api"),
    )
    assert is_hr is True


def test_shell_normal_command_is_not_high_risk():
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "shell", ("shell", "exec", "ls -la"),
    )
    assert is_hr is False


def test_ssh_exec_force_push_is_high_risk():
    """Remote regret-pattern commands are even more important to flag."""
    from janus.cli_rich import _is_high_risk_grant
    is_hr, _ = _is_high_risk_grant(
        "ssh_exec", ("ssh", "exec", "git push --force origin main"),
    )
    assert is_hr is True


def test_no_capability_is_not_high_risk():
    """When capability is missing, we have no target to evaluate —
    fall through to the standard prompt rather than over-narrowing."""
    from janus.cli_rich import _is_high_risk_grant
    assert _is_high_risk_grant("fs_write", None) == (False, "")
    assert _is_high_risk_grant("fs_write", ()) == (False, "")


# ---------- approver behavior under high-risk ----------


class _FakeConsole:
    """Minimal Console double — captures Panel rendering + line prints."""
    def __init__(self):
        self.printed = []

    def print(self, *args, **kwargs):
        self.printed.append((args, kwargs))


def _build_approver(monkeypatch, mode="default", input_answer="y"):
    """Construct the cli_rich approver with input() stubbed to
    `input_answer`. prompt_toolkit is forced off so we hit the
    plain-input branch deterministically."""
    from janus import cli_rich, permissions
    monkeypatch.setattr(cli_rich, "HAVE_RICH", False)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: input_answer)
    console = _FakeConsole()
    state = permissions.ModeState(current=mode)
    return cli_rich._make_mode_approver(console, state), state, console


def test_high_risk_prompt_uses_yes_no_only(monkeypatch):
    """The prompt text rendered should only offer Y/N when high-risk."""
    seen = {}
    def fake_input(prompt):
        seen["prompt"] = prompt
        return "n"
    monkeypatch.setattr("builtins.input", fake_input)
    from janus import cli_rich, permissions
    monkeypatch.setattr(cli_rich, "HAVE_RICH", False)
    state = permissions.ModeState(current="default")
    approver = cli_rich._make_mode_approver(_FakeConsole(), state)
    approver(
        "fs_write: create",
        "create docs/X.md (100 bytes)",
        risk="write",
        tool_name="fs_write",
        capability=("fs", "write", "docs/X.md"),
    )
    assert "[Y]es" in seen["prompt"]
    assert "[N]o" in seen["prompt"]
    assert "[s]ession" not in seen["prompt"]
    assert "[a]lways" not in seen["prompt"]


def test_normal_prompt_keeps_session_always(monkeypatch):
    seen = {}
    def fake_input(prompt):
        seen["prompt"] = prompt
        return "n"
    monkeypatch.setattr("builtins.input", fake_input)
    from janus import cli_rich, permissions
    monkeypatch.setattr(cli_rich, "HAVE_RICH", False)
    state = permissions.ModeState(current="default")
    approver = cli_rich._make_mode_approver(_FakeConsole(), state)
    approver(
        "fs_write: create",
        "create src/foo.py (100 bytes)",
        risk="write",
        tool_name="fs_write",
        capability=("fs", "write", "src/foo.py"),
    )
    assert "[s]ession" in seen["prompt"]
    assert "[a]lways" in seen["prompt"]


def test_high_risk_session_answer_declined(monkeypatch):
    """Even if the user types `s`, a high-risk request must NOT add a
    session grant — `s` is treated as decline + explanatory message."""
    approver, state, console = _build_approver(
        monkeypatch, input_answer="s",
    )
    out = approver(
        "fs_write: create",
        "create docs/X.md",
        risk="write",
        tool_name="fs_write",
        capability=("fs", "write", "docs/X.md"),
    )
    assert out is False
    assert ("fs_write", "write") not in state.session_grants


def test_high_risk_always_answer_declined(monkeypatch):
    approver, state, console = _build_approver(
        monkeypatch, input_answer="a",
    )
    out = approver(
        "fs_write: create",
        "create docs/X.md",
        risk="write",
        tool_name="fs_write",
        capability=("fs", "write", "docs/X.md"),
    )
    assert out is False
    # No persistent grant should have been written either.
    assert ("fs_write", "write") not in state.session_grants


def test_high_risk_yes_still_approves_once(monkeypatch):
    approver, state, _ = _build_approver(monkeypatch, input_answer="y")
    out = approver(
        "fs_write: create",
        "create docs/X.md",
        risk="write",
        tool_name="fs_write",
        capability=("fs", "write", "docs/X.md"),
    )
    assert out is True
    # Critical: yes-once does NOT promote to a session grant.
    assert ("fs_write", "write") not in state.session_grants


def test_existing_session_grant_does_not_silently_pass_high_risk(monkeypatch):
    """Sam's worst case: he typed `s` for a normal write earlier,
    granting (fs_write, write). Then the model proposes a docs/ write.
    The pre-existing grant must NOT auto-approve — we must re-prompt."""
    seen = {}
    def fake_input(prompt):
        seen["prompt"] = prompt
        return "n"
    monkeypatch.setattr("builtins.input", fake_input)
    from janus import cli_rich, permissions
    monkeypatch.setattr(cli_rich, "HAVE_RICH", False)
    state = permissions.ModeState(current="default")
    state.grant(("fs_write", "write"))  # earlier session grant
    approver = cli_rich._make_mode_approver(_FakeConsole(), state)
    out = approver(
        "fs_write: create",
        "create docs/X.md",
        risk="write",
        tool_name="fs_write",
        capability=("fs", "write", "docs/X.md"),
    )
    # The user said `n` at the re-prompt → False.
    assert out is False
    # And the prompt must have actually appeared (not silent-pass).
    assert "prompt" in seen


def test_existing_grant_still_passes_normal_paths(monkeypatch):
    """Regression: the new gating must not break the common path —
    a session grant for fs_write should still auto-approve writes
    to non-protected paths."""
    from janus import cli_rich, permissions
    monkeypatch.setattr(cli_rich, "HAVE_RICH", False)
    state = permissions.ModeState(current="default")
    state.grant(("fs_write", "write"))
    approver = cli_rich._make_mode_approver(_FakeConsole(), state)
    # No input() stub — if the approver tried to prompt, this test
    # would hang. Reaching the assertion proves it didn't.
    out = approver(
        "fs_write: create",
        "create src/foo.py",
        risk="write",
        tool_name="fs_write",
        capability=("fs", "write", "src/foo.py"),
    )
    assert out is True
