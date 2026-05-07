"""Tests for v1.28.1 — skills auto-offer UX polish.

v1.28.0 shipped detection + drafting via slash commands. v1.28.1
wires an auto-surfaced one-line offer into cli_rich's after-turn
flow. Cooldown'd so it doesn't nag — once per pattern per 7 days.
Higher occurrence threshold than detection itself because drafting
is an LLM call we don't want to nudge toward without a strong
signal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import config, skill_proposer


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    skills_dir = home / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()
    return home


# ============================================================
# Constant + env override
# ============================================================


def test_auto_offer_min_occurrences_constant_exists():
    assert hasattr(skill_proposer, "AUTO_OFFER_MIN_OCCURRENCES")
    assert isinstance(skill_proposer.AUTO_OFFER_MIN_OCCURRENCES, int)


def test_auto_offer_min_occurrences_default_above_detection():
    """Auto-offer threshold must be HIGHER than detection thresholds —
    we don't want to nudge a draft for borderline patterns."""
    assert (
        skill_proposer.AUTO_OFFER_MIN_OCCURRENCES
        >= skill_proposer.SEQ_MIN_OCCURRENCES
    )


def test_auto_offer_threshold_default_is_four():
    """Default 4 — well above detection threshold (3) and a sane
    'this is happening enough to matter' line."""
    assert skill_proposer.AUTO_OFFER_MIN_OCCURRENCES == 4


def test_auto_offer_constant_in_all_export():
    assert "AUTO_OFFER_MIN_OCCURRENCES" in skill_proposer.__all__


# ============================================================
# cli_rich source-pin: auto-offer wired into chat loop
# ============================================================


_AUTO_OFFER_MARKER = "v1.28.1: skill-proposal auto-offer"


def test_cli_rich_auto_offer_block_present():
    """The chat loop must call skill_proposer.list_offerable AFTER
    the turn and before the next prompt, so the user sees the offer
    inline rather than at session end."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert _AUTO_OFFER_MARKER in src, "v1.28.1 auto-offer marker missing"
    # The auto-offer block lives after the inferred-memory offer
    inf_idx = src.find("interview_inferred")
    sp_idx = src.find(_AUTO_OFFER_MARKER)
    assert -1 < inf_idx < sp_idx, (
        "skill auto-offer must come after inferred-memory offer in cli_rich"
    )


def test_cli_rich_auto_offer_uses_threshold_constant():
    """Source-pin: the offer block compares against
    AUTO_OFFER_MIN_OCCURRENCES, not a magic number — so env override
    actually changes behavior."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "AUTO_OFFER_MIN_OCCURRENCES" in src


def test_cli_rich_auto_offer_marks_offered():
    """After surfacing the offer we mark_offered so the cooldown
    starts — otherwise the same pattern would fire on every turn."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    # Search the auto-offer block region specifically
    sp_idx = src.find(_AUTO_OFFER_MARKER)
    # Inside the cli_rich wiring, mark_offered must appear after the
    # skill_proposer import.
    region = src[sp_idx:sp_idx + 2000]
    assert "mark_offered" in region


def test_cli_rich_auto_offer_wrapped_in_try_except():
    """The auto-offer must NEVER crash the chat loop. Wrapped in
    try/except Exception. The try: appears just AFTER the marker
    comment that names this block."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    sp_idx = src.find(_AUTO_OFFER_MARKER)
    region = src[sp_idx:sp_idx + 2000]
    assert "try:" in region
    assert "except Exception:" in region


def test_cli_rich_auto_offer_respects_top_only():
    """Only the highest-occurrence pattern fires per turn — keeps
    the offer block to one line, never a stack of offers."""
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    sp_idx = src.find(_AUTO_OFFER_MARKER)
    region = src[sp_idx:sp_idx + 2000]
    # We index patterns[0] (top), don't iterate
    assert "patterns[0]" in region
    # And we DO NOT loop over the patterns list with a for in this block
    # (a for-loop would multi-offer)
    block_lines = region.split("\n")[:30]
    has_iter = any(
        ln.strip().startswith("for ") and "patterns" in ln
        for ln in block_lines
    )
    assert not has_iter, "auto-offer must not iterate; one offer per turn"


# ============================================================
# Cooldown integration: auto-offer respects existing state
# ============================================================


def test_cooldown_blocks_repeat_offers(tmp_path, monkeypatch):
    """list_offerable filters by cooldown — so once mark_offered
    fires, the same pattern won't be in the offerable list next
    call until the cooldown elapses."""
    _isolate(tmp_path, monkeypatch)
    p = skill_proposer.Pattern(
        id="test-pattern", kind="x", description="d", occurrences=10,
    )
    skill_proposer.mark_offered(p.id)
    out = skill_proposer.filter_offerable([p])
    assert out == []


def test_accepted_pattern_never_re_offers(tmp_path, monkeypatch):
    """Once a user /skills propose-d (which calls mark_accepted),
    the pattern is permanently filtered. Cooldown isn't enough —
    the skill already exists."""
    _isolate(tmp_path, monkeypatch)
    p = skill_proposer.Pattern(
        id="accepted-p", kind="x", description="d", occurrences=10,
    )
    skill_proposer.mark_accepted(p.id)
    out = skill_proposer.filter_offerable([p])
    assert out == []


# ============================================================
# Threshold env-override (subprocess — module-level reload would
# leak state into other test files)
# ============================================================


def test_threshold_env_override_via_subprocess(tmp_path):
    """``JANUS_SKILL_AUTO_OFFER_MIN_OCCURRENCES`` is read at module
    import. Use a fresh subprocess so the env var actually applies
    — importlib.reload in-process would leak state into downstream
    tests (e.g. memory_index DB pointers)."""
    import subprocess
    import sys
    import os

    env = dict(os.environ)
    env["JANUS_SKILL_AUTO_OFFER_MIN_OCCURRENCES"] = "2"
    proc = subprocess.run(
        [
            sys.executable, "-c",
            (
                "from janus import skill_proposer; "
                "print(skill_proposer.AUTO_OFFER_MIN_OCCURRENCES)"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "2"


def test_threshold_env_invalid_falls_back_via_subprocess(tmp_path):
    import subprocess
    import sys
    import os

    env = dict(os.environ)
    env["JANUS_SKILL_AUTO_OFFER_MIN_OCCURRENCES"] = "not-a-number"
    proc = subprocess.run(
        [
            sys.executable, "-c",
            (
                "from janus import skill_proposer; "
                "print(skill_proposer.AUTO_OFFER_MIN_OCCURRENCES)"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "4"  # fallback to default
