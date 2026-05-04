"""
onboarding.py — first-run interactive setup wizard (v1.13.0).

WHY THIS EXISTS:
A new user installing Janus has to know about JANUS_API_KEY,
JANUS_API_BASE, JANUS_MODEL, ~/.janus/.env file format, the bundled
skill catalog, the daemon for scheduled agents, etc. Pre-v1.13 they
got this from `janus --help` + reading the README. That works for
contributors. For a new user it's a wall of unknowns.

Hermes calls their version `agent/onboarding.py`. v1.13 ports the
spirit: ONE command (`janus onboard`) walks the user through:

  1. Pick a provider (Nous Portal / OpenRouter / OpenAI / Anthropic /
     local / other-OpenAI-compatible)
  2. Enter API key (stored in ~/.janus/.env)
  3. Pick a model (cheap default by provider; opt-in to a bigger one)
  4. Optional: install the bundled skill catalog (~/.janus/skills/)
  5. Optional: configure Telegram (token + allowed chats)
  6. Optional: set permission mode default

DESIGN — STDIN PROMPTS, NO TUI DEPS:
We don't pull in prompt_toolkit / questionary / inquirer just for
this. plain `input()` with numbered options is enough. Failure-silent:
ctrl-c at any prompt aborts cleanly without trashing partial state.

WHERE STATE LANDS:
  - ~/.janus/.env — API_KEY, API_BASE, MODEL (the trio every other
    module reads)
  - JANUS_TELEGRAM_TOKEN, JANUS_TELEGRAM_CHATS (when wired)
  - JANUS_APPROVAL (the chosen default mode)

We APPEND to the .env so existing keys aren't blown away. If a key
already has a value, the wizard asks before overwriting.

P5 (plain-text state): the .env is the user's file — they can edit it
directly any time. Wizard is a convenience, not a gate.
"""

from __future__ import annotations
import os
import shutil
from pathlib import Path
from typing import Any

from . import config


# ---------- Provider catalog ----------


PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "OpenRouter",
        "api_base": "https://openrouter.ai/api/v1",
        "key_env": "JANUS_API_KEY",
        "key_url": "https://openrouter.ai/keys",
        "default_model": "openai/gpt-4o-mini",
        "popular_models": [
            "anthropic/claude-haiku-4-5",
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-4o-mini",
            "google/gemini-2.0-flash",
            "deepseek/deepseek-chat",
        ],
        "blurb": "200+ models, pay-per-use, no commitment. Recommended for new users.",
    },
    {
        "name": "Anthropic",
        "api_base": "https://api.anthropic.com/v1",
        "key_env": "JANUS_API_KEY",
        "key_url": "https://console.anthropic.com/settings/keys",
        "default_model": "claude-haiku-4-5",
        "popular_models": [
            "claude-haiku-4-5",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
        ],
        "blurb": "Direct Anthropic API. Best Claude pricing.",
    },
    {
        "name": "OpenAI",
        "api_base": "https://api.openai.com/v1",
        "key_env": "JANUS_API_KEY",
        "key_url": "https://platform.openai.com/api-keys",
        "default_model": "gpt-4o-mini",
        "popular_models": ["gpt-4o-mini", "gpt-4o", "o1-mini"],
        "blurb": "Direct OpenAI API.",
    },
    {
        "name": "Nous Portal",
        "api_base": "https://inference-api.nousresearch.com/v1",
        "key_env": "JANUS_API_KEY",
        "key_url": "https://portal.nousresearch.com",
        "default_model": "Hermes-3-Llama-3.1-8B",
        "popular_models": [
            "Hermes-3-Llama-3.1-8B",
            "Hermes-3-Llama-3.1-70B",
        ],
        "blurb": "Nous Research's hosted inference. Hermes models.",
    },
    {
        "name": "Local (Ollama / llama.cpp / LM Studio)",
        "api_base": "http://localhost:11434/v1",
        "key_env": "JANUS_API_KEY",
        "key_url": None,
        "default_model": "llama3.2",
        "popular_models": ["llama3.2", "qwen2.5", "mistral"],
        "blurb": "OpenAI-compatible local inference. Free, runs on your hardware.",
    },
    {
        "name": "Other OpenAI-compatible",
        "api_base": None,
        "key_env": "JANUS_API_KEY",
        "key_url": None,
        "default_model": "",
        "popular_models": [],
        "blurb": "Custom endpoint. You'll enter the base URL + model id.",
    },
]


# ---------- Wizard ----------


def run_wizard(*, prompt=input, output=print) -> bool:
    """Run the interactive wizard. Returns True if setup completed,
    False if the user aborted partway. `prompt` and `output` are
    overridable for tests (they default to stdin / stdout)."""
    try:
        return _run(prompt=prompt, output=output)
    except (EOFError, KeyboardInterrupt):
        output("\nonboarding cancelled — partial state may have been written to ~/.janus/.env")
        return False


def _run(*, prompt, output) -> bool:
    config.ensure_home()
    env_path = config.HOME / ".env"
    output("\n" + "=" * 60)
    output("Janus onboarding")
    output("=" * 60)
    output(
        "This wizard configures the minimum to start using Janus. "
        "Anything you skip can be set later by editing ~/.janus/.env "
        "directly. Press Ctrl+C at any time to abort.\n"
    )

    # Step 1 — provider.
    provider = _pick_provider(prompt=prompt, output=output)
    if provider is None:
        return False

    # Step 2 — api base.
    api_base = provider["api_base"]
    if api_base is None:
        api_base = (prompt(
            "API base URL (e.g., https://api.something.com/v1): "
        ) or "").strip()
        if not api_base:
            output("no API base — aborting")
            return False

    # Step 3 — api key.
    output(f"\nAPI key (stored in ~/.janus/.env, never printed back):")
    if provider.get("key_url"):
        output(f"  Get one: {provider['key_url']}")
    api_key = (prompt("> ") or "").strip()
    if not api_key:
        output("no API key — aborting")
        return False

    # Step 4 — model.
    model = _pick_model(provider, prompt=prompt, output=output)
    if not model:
        return False

    # Step 5 — write to .env (append-or-update).
    _upsert_env(env_path, {
        "JANUS_API_BASE": api_base,
        "JANUS_API_KEY": api_key,
        "JANUS_MODEL": model,
    })
    output(f"\nwrote provider config to {env_path}")

    # Step 6 — permission mode default.
    _maybe_set_mode(env_path, prompt=prompt, output=output)

    # Step 7 — optional: bundled skills.
    _maybe_install_skills(prompt=prompt, output=output)

    # Step 8 — optional: telegram.
    _maybe_setup_telegram(env_path, prompt=prompt, output=output)

    output("\n" + "=" * 60)
    output("Done. Try `janus` to start chatting.")
    output("=" * 60 + "\n")
    return True


# ---------- Steps ----------


def _pick_provider(*, prompt, output) -> dict[str, Any] | None:
    output("Choose a provider:\n")
    for i, p in enumerate(PROVIDERS, 1):
        output(f"  {i}. {p['name']} — {p['blurb']}")
    output("")
    raw = (prompt("> ") or "").strip()
    if not raw:
        output("no provider selected — aborting")
        return None
    try:
        idx = int(raw) - 1
    except ValueError:
        output(f"unrecognized choice {raw!r} — aborting")
        return None
    if not 0 <= idx < len(PROVIDERS):
        output(f"out of range — aborting")
        return None
    return PROVIDERS[idx]


def _pick_model(provider: dict[str, Any], *, prompt, output) -> str:
    pop = provider.get("popular_models") or []
    if pop:
        output(f"\nPopular models for {provider['name']}:")
        for i, m in enumerate(pop, 1):
            marker = "  ← default" if m == provider.get("default_model") else ""
            output(f"  {i}. {m}{marker}")
        output("  Or type a custom model id.")
        raw = (prompt("> ") or "").strip()
        if not raw:
            return provider.get("default_model", "")
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(pop):
                return pop[idx]
        except ValueError:
            pass
        # Treat as custom model id.
        return raw
    # Fall-through (custom provider): just ask.
    raw = (prompt("Model id: ") or "").strip()
    return raw


def _maybe_set_mode(env_path: Path, *, prompt, output) -> None:
    output("\nDefault permission mode:")
    output("  1. default — asks before write/exec actions (safest)")
    output("  2. acceptEdits — auto-allows writes, asks for exec")
    output("  3. plan — read-only (good for first sessions)")
    output("  4. auto — auto-allows but blocks dangerous patterns")
    output("  5. bypassPermissions — no safety net (NOT recommended)")
    output("  Press Enter to skip (default: 'default')")
    raw = (prompt("> ") or "").strip()
    mapping = {
        "1": "default", "2": "acceptEdits", "3": "plan",
        "4": "auto", "5": "bypassPermissions",
    }
    mode = mapping.get(raw)
    if mode:
        _upsert_env(env_path, {"JANUS_APPROVAL": mode})
        output(f"  default mode set to: {mode}")


def _maybe_install_skills(*, prompt, output) -> None:
    output(
        "\nInstall the bundled skill catalog (50+ skills like gh-pr, "
        "fs-grep, agent-self-portrait)?  [y/N]"
    )
    raw = (prompt("> ") or "").strip().lower()
    if raw not in ("y", "yes"):
        return
    try:
        from . import skill_catalog
        results = skill_catalog.install_bundled(force=False)
        installed = [r for r in results if r.action == "installed"]
        skipped = [r for r in results if r.action == "skipped"]
        output(f"  installed {len(installed)} skill(s), skipped {len(skipped)}")
    except Exception as e:
        output(f"  skill install failed: {type(e).__name__}: {e}")


def _maybe_setup_telegram(env_path: Path, *, prompt, output) -> None:
    output(
        "\nSet up the Telegram gateway? You'll need a bot token from "
        "@BotFather. [y/N]"
    )
    raw = (prompt("> ") or "").strip().lower()
    if raw not in ("y", "yes"):
        return
    token = (prompt("Bot token (e.g., 1234567890:ABC...): ") or "").strip()
    if not token:
        output("  no token — skipping telegram setup")
        return
    chats = (prompt(
        "Allowed chat IDs, comma-separated (or empty to allow pairing): "
    ) or "").strip()
    updates: dict[str, str] = {"JANUS_TELEGRAM_TOKEN": token}
    if chats:
        updates["JANUS_TELEGRAM_CHATS"] = chats
    _upsert_env(env_path, updates)
    output("  telegram config saved. Run: `janus telegram`")


# ---------- .env helpers ----------


def _upsert_env(path: Path, updates: dict[str, str]) -> None:
    """Add/replace keys in a dotenv file. Preserves comments + ordering
    of unrelated lines. Idempotent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    seen: set[str] = set()
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing = []
    out_lines: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_val = updates[key]
            out_lines.append(f"{key}={_dotenv_quote(new_val)}")
            seen.add(key)
        else:
            out_lines.append(line)
    # Append any keys we didn't already replace.
    for key, val in updates.items():
        if key not in seen:
            out_lines.append(f"{key}={_dotenv_quote(val)}")
    text = "\n".join(out_lines).rstrip() + "\n"
    path.write_text(text, encoding="utf-8")


def _dotenv_quote(value: str) -> str:
    """Quote a value for the .env file. Wraps in double-quotes if it
    contains spaces or special chars."""
    if not value:
        return '""'
    needs = any(c in value for c in [" ", "\t", "#", "$", "'", '"', "\\"])
    if needs:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
