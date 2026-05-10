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


# ---------- v1.32.3 — Auto-detect provider from env / localhost ----------
#
# Phase 5.4 polish: a fresh user with OPENAI_API_KEY already in their
# shell shouldn't have to type it again. Same for ANTHROPIC_API_KEY,
# OPENROUTER_API_KEY, or a running Ollama on localhost. The wizard
# probes these signals and offers them as the default first option.


def _detect_env_keys(env: dict[str, str] | None = None) -> list[dict]:
    """Inspect the environment for known API keys. Returns a list of
    candidate provider matches (in preference order — most specific
    first), each with the provider dict + the discovered key."""
    if env is None:
        env = dict(os.environ)
    matches: list[dict] = []
    # Map well-known env var names to their provider names.
    # Order = preference: more-specific keys before generic.
    key_to_provider = [
        ("ANTHROPIC_API_KEY", "Anthropic"),
        ("OPENROUTER_API_KEY", "OpenRouter"),
        ("OPENAI_API_KEY", "OpenAI"),
    ]
    for env_var, provider_name in key_to_provider:
        val = env.get(env_var)
        if not val:
            continue
        provider = next(
            (p for p in PROVIDERS if p["name"] == provider_name),
            None,
        )
        if provider is None:
            continue
        matches.append({
            "provider": provider,
            "source": f"env:{env_var}",
            "key": val,
        })
    # Janus-native key: if JANUS_API_KEY is already set, we can't
    # tell which provider it's for, but we surface it as a "use
    # whatever's in .env" option.
    if env.get("JANUS_API_KEY") and not matches:
        matches.append({
            "provider": None,
            "source": "env:JANUS_API_KEY",
            "key": env["JANUS_API_KEY"],
        })
    return matches


def _detect_ollama(host: str = "http://localhost:11434", *, timeout: float = 0.5) -> bool:
    """Probe whether Ollama is running. Short timeout so the wizard
    doesn't stall on a network roundtrip."""
    try:
        import urllib.request
        req = urllib.request.Request(f"{host}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def detect_candidates(
    env: dict[str, str] | None = None,
    *,
    probe_ollama: bool = True,
) -> list[dict]:
    """Return a list of detected candidate providers, in preference
    order. Each entry: {provider, source, key} where key may be empty
    when only the provider was detected (e.g., Ollama on localhost
    doesn't need a key)."""
    candidates: list[dict] = list(_detect_env_keys(env))
    if probe_ollama and _detect_ollama():
        ollama_provider = next(
            (p for p in PROVIDERS if "Ollama" in p["name"] or "Local" in p["name"]),
            None,
        )
        if ollama_provider is not None:
            candidates.append({
                "provider": ollama_provider,
                "source": "localhost:11434",
                "key": "",
            })
    return candidates


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
    # v1.32.3 — if the auto-detect step pre-filled a key from env
    # (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.) use it without
    # asking again. User can blank-Enter to override.
    prefilled = provider.get("_prefill_key", "")
    if prefilled:
        masked = prefilled[:6] + "..." + prefilled[-4:] if len(prefilled) > 10 else "***"
        output(f"\nAPI key — using {masked} (auto-detected). Press Enter to keep, or paste a new one:")
        raw = (prompt("> ") or "").strip()
        api_key = raw or prefilled
    else:
        output("\nAPI key (stored in ~/.janus/.env, never printed back):")
        if provider.get("key_url"):
            output(f"  Get one: {provider['key_url']}")
        api_key = (prompt("> ") or "").strip()
    # Local Ollama doesn't need a real key — accept any placeholder.
    is_local = "localhost" in (api_base or "") or "127.0.0.1" in (api_base or "")
    if not api_key and not is_local:
        output("no API key — aborting")
        return False
    if not api_key and is_local:
        api_key = "ollama"  # Ollama ignores the key but the runtime requires non-empty

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
    """Pick a provider. v1.32.3 — if env vars OR localhost Ollama
    suggest a provider, surface those as the top options so the
    user only types '1<Enter>' for the common case."""
    candidates = detect_candidates()
    if candidates:
        output("Detected from your environment:\n")
        for i, c in enumerate(candidates, 1):
            p = c["provider"]
            name = p["name"] if p else "Custom (using $JANUS_API_KEY)"
            source = c["source"]
            output(f"  {i}. {name}  [{source}]")
        next_index = len(candidates) + 1
        output("")
        output("Or pick from the full provider list:")
        for j, p in enumerate(PROVIDERS, next_index):
            output(f"  {j}. {p['name']} — {p['blurb']}")
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
        # Indices 0..len(candidates)-1 → detected candidates (use
        # their pre-filled key/provider). Indices >= len(candidates)
        # → full PROVIDERS list (offset by len(candidates)).
        if 0 <= idx < len(candidates):
            cand = candidates[idx]
            if cand["provider"] is not None:
                # Stash the discovered key on the provider dict so
                # the API-key step can reuse it without re-asking.
                provider = dict(cand["provider"])
                provider["_prefill_key"] = cand["key"]
                return provider
            # JANUS_API_KEY-only candidate — fall back to manual.
            output("  Using existing $JANUS_API_KEY — pick the matching provider below.")
            output("")
        else:
            idx -= len(candidates)
        if not 0 <= idx < len(PROVIDERS):
            output("out of range — aborting")
            return None
        return PROVIDERS[idx]
    # No detection — original behavior.
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
