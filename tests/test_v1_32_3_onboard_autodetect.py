"""Tests for v1.32.3 — onboarding wizard auto-detect (Phase 5.4).

WHAT THIS SHIPS:
First-run polish. The wizard now probes the environment for known
API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, OPENROUTER_API_KEY,
JANUS_API_KEY) AND localhost:11434 for Ollama. Detected providers
appear at the top of the picker so a user with creds already
exported just types '1<Enter>' instead of going through the full
provider list + re-typing their key.

DESIGN INVARIANTS PINPOINTED:
  * detect_candidates() returns a list[dict] (provider, source, key)
  * Empty env, no Ollama → empty list
  * OPENAI_API_KEY set → OpenAI candidate
  * ANTHROPIC_API_KEY set → Anthropic candidate
  * OPENROUTER_API_KEY set → OpenRouter candidate
  * Multiple keys → all surface, in preference order (Anthropic >
    OpenRouter > OpenAI; matches the kind of user who'd have
    multiple)
  * JANUS_API_KEY alone → "use what's there" candidate (no
    matching provider)
  * Ollama probe is short-timeout (doesn't stall the wizard)
  * The wizard's _pick_provider surfaces detected first, falls
    back to the full PROVIDERS list afterward
  * Auto-detected key is masked when shown (first 6 + last 4 chars)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from janus import onboarding


# -------------------- detect_candidates() --------------------


def test_detect_no_keys_no_ollama(monkeypatch):
    """Clean environment + no Ollama → empty list."""
    # Probe Ollama returns False
    with patch.object(onboarding, "_detect_ollama", return_value=False):
        candidates = onboarding.detect_candidates(env={}, probe_ollama=True)
    assert candidates == []


def test_detect_openai_key(monkeypatch):
    """OPENAI_API_KEY set → OpenAI candidate, source=env:OPENAI_API_KEY."""
    with patch.object(onboarding, "_detect_ollama", return_value=False):
        candidates = onboarding.detect_candidates(
            env={"OPENAI_API_KEY": "sk-test123"},
            probe_ollama=True,
        )
    assert len(candidates) == 1
    assert candidates[0]["provider"]["name"] == "OpenAI"
    assert candidates[0]["source"] == "env:OPENAI_API_KEY"
    assert candidates[0]["key"] == "sk-test123"


def test_detect_anthropic_key():
    with patch.object(onboarding, "_detect_ollama", return_value=False):
        candidates = onboarding.detect_candidates(
            env={"ANTHROPIC_API_KEY": "sk-ant-test"},
            probe_ollama=True,
        )
    assert len(candidates) == 1
    assert candidates[0]["provider"]["name"] == "Anthropic"


def test_detect_openrouter_key():
    with patch.object(onboarding, "_detect_ollama", return_value=False):
        candidates = onboarding.detect_candidates(
            env={"OPENROUTER_API_KEY": "sk-or-test"},
            probe_ollama=True,
        )
    assert len(candidates) == 1
    assert candidates[0]["provider"]["name"] == "OpenRouter"


def test_detect_multiple_keys_in_priority_order():
    """Anthropic > OpenRouter > OpenAI when all three are present."""
    with patch.object(onboarding, "_detect_ollama", return_value=False):
        candidates = onboarding.detect_candidates(
            env={
                "ANTHROPIC_API_KEY": "sk-ant",
                "OPENROUTER_API_KEY": "sk-or",
                "OPENAI_API_KEY": "sk-oai",
            },
            probe_ollama=True,
        )
    names = [c["provider"]["name"] for c in candidates]
    assert names == ["Anthropic", "OpenRouter", "OpenAI"]


def test_detect_janus_api_key_alone():
    """JANUS_API_KEY without a provider-specific key → 'unknown
    provider' candidate so user can pick the matching provider
    manually but still reuse the existing key."""
    with patch.object(onboarding, "_detect_ollama", return_value=False):
        candidates = onboarding.detect_candidates(
            env={"JANUS_API_KEY": "some-key"},
            probe_ollama=True,
        )
    assert len(candidates) == 1
    assert candidates[0]["provider"] is None
    assert candidates[0]["source"] == "env:JANUS_API_KEY"
    assert candidates[0]["key"] == "some-key"


def test_detect_specific_keys_win_over_janus_api_key():
    """If both OPENAI_API_KEY and JANUS_API_KEY are set, surface
    OpenAI as the more-specific match. JANUS_API_KEY-only is the
    fallback for users who already manually wired one."""
    with patch.object(onboarding, "_detect_ollama", return_value=False):
        candidates = onboarding.detect_candidates(
            env={
                "OPENAI_API_KEY": "sk-oai",
                "JANUS_API_KEY": "duplicate-key",
            },
            probe_ollama=True,
        )
    # JANUS_API_KEY shouldn't surface as a generic candidate when a
    # specific provider is already detected.
    sources = [c["source"] for c in candidates]
    assert "env:OPENAI_API_KEY" in sources
    assert "env:JANUS_API_KEY" not in sources


def test_detect_ollama_when_running():
    """Ollama probe returning True surfaces the Local provider."""
    with patch.object(onboarding, "_detect_ollama", return_value=True):
        candidates = onboarding.detect_candidates(
            env={},
            probe_ollama=True,
        )
    assert len(candidates) == 1
    assert "Local" in candidates[0]["provider"]["name"] or \
        "Ollama" in candidates[0]["provider"]["name"]
    assert candidates[0]["source"] == "localhost:11434"
    # No key needed for local
    assert candidates[0]["key"] == ""


def test_detect_ollama_disabled():
    """probe_ollama=False skips the network call entirely."""
    # Patch _detect_ollama to raise — proving probe_ollama=False
    # bypasses the call.
    with patch.object(onboarding, "_detect_ollama", side_effect=AssertionError("called")):
        candidates = onboarding.detect_candidates(
            env={},
            probe_ollama=False,
        )
    assert candidates == []


def test_detect_combined_env_key_and_ollama():
    """Both env key AND Ollama → both surface, in the order they
    were added (env first, then Ollama)."""
    with patch.object(onboarding, "_detect_ollama", return_value=True):
        candidates = onboarding.detect_candidates(
            env={"OPENAI_API_KEY": "sk-oai"},
            probe_ollama=True,
        )
    assert len(candidates) == 2
    # Env match comes first
    assert candidates[0]["provider"]["name"] == "OpenAI"
    # Ollama second
    assert "Local" in candidates[1]["provider"]["name"] or \
        "Ollama" in candidates[1]["provider"]["name"]


# -------------------- _detect_ollama probe --------------------


def test_detect_ollama_returns_false_when_unreachable():
    """Probe with a tight timeout against a port nothing is listening
    on returns False without raising."""
    # Use a high port that's almost certainly free.
    result = onboarding._detect_ollama("http://127.0.0.1:1", timeout=0.05)
    assert result is False


# -------------------- _pick_provider surfaces detected --------------------


def test_pick_provider_surfaces_detected_first():
    """When env has OPENAI_API_KEY, _pick_provider lists OpenAI as
    option 1 (above the full PROVIDERS list)."""
    outputs: list[str] = []

    def fake_prompt(_prompt):
        return "1"

    def fake_output(*args):
        outputs.append(" ".join(str(a) for a in args))

    with patch.object(onboarding, "_detect_ollama", return_value=False), \
         patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False):
        # We need to clear other potential keys for a clean test.
        with patch.object(
            onboarding, "_detect_env_keys",
            return_value=[{
                "provider": next(p for p in onboarding.PROVIDERS if p["name"] == "OpenAI"),
                "source": "env:OPENAI_API_KEY",
                "key": "sk-test",
            }]
        ):
            chosen = onboarding._pick_provider(prompt=fake_prompt, output=fake_output)

    assert chosen is not None
    assert chosen["name"] == "OpenAI"
    # Key was prefilled
    assert chosen.get("_prefill_key") == "sk-test"
    # Output mentions "Detected from your environment"
    blob = "\n".join(outputs)
    assert "Detected" in blob


def test_pick_provider_falls_through_to_full_list_when_no_detection():
    """No env keys, no Ollama → original behavior (full PROVIDERS
    list directly)."""
    outputs: list[str] = []

    def fake_prompt(_prompt):
        return "1"

    def fake_output(*args):
        outputs.append(" ".join(str(a) for a in args))

    with patch.object(onboarding, "detect_candidates", return_value=[]):
        chosen = onboarding._pick_provider(prompt=fake_prompt, output=fake_output)

    assert chosen is not None
    # Without detection, option 1 is the first in PROVIDERS (OpenRouter)
    assert chosen["name"] == onboarding.PROVIDERS[0]["name"]
    # Output should NOT mention "Detected"
    blob = "\n".join(outputs)
    assert "Detected" not in blob


# -------------------- Version pin --------------------


def test_version_bumped_to_1_32_3_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 32, 3)
