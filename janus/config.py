"""config.py -- single source of truth for paths, env vars, and tunables."""

from __future__ import annotations
import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from `path` into os.environ (set-if-missing).

    Tiny parser, no `python-dotenv` dep (P6). Lines starting with '#' are
    comments. Quoted values have the outer quotes stripped. Existing env
    vars (shell exports) are NEVER overridden.
    """
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            ):
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


# .env discovery: cwd first (per-project), then ~/.janus/.env (global default).
# Existing shell exports always win.
_load_dotenv(Path.cwd() / ".env")
_load_dotenv(Path.home() / ".janus" / ".env")

# --- Provider configuration ---
API_KEY: str = os.getenv("JANUS_API_KEY", "")
API_BASE: str = os.getenv("JANUS_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
MODEL: str = os.getenv("JANUS_MODEL", "openai/gpt-4o-mini")

# --- Where state lives ---
HOME: Path = Path(os.getenv("JANUS_HOME", str(Path.home() / ".janus")))
LOG_FILE: Path = HOME / "log.jsonl"
SESSIONS_DB: Path = HOME / "sessions.db"
USER_MODEL_FILE: Path = HOME / "user.md"  # legacy, migrated to MEMORY_DIR/user.md
MEMORY_DIR: Path = HOME / "memory"
SKILLS_DIR: Path = HOME / "skills"
EVALS_DIR: Path = HOME / "evals"
TRIGGERS_DIR: Path = HOME / "triggers"
DAEMON_STATE: Path = HOME / "daemon.state.json"
HISTORY_FILE: Path = HOME / "cli_history"

# --- Workspace ---
WORKSPACE: Path = Path(os.getenv("JANUS_WORKSPACE", str(Path.cwd()))).resolve()

# --- Loop limits ---
# v1.20: step-budget redesign. Single hard counter (MAX_STEPS=25) was
# tripping mid-task on multi-stage workflows. Replaced with soft/hard
# caps + progress-aware extension. Behavior:
#   0..STEP_SOFT_CAP   : normal operation
#   STEP_SOFT_CAP      : inject "wrap up" reminder once
#   ..STEP_HARD_CAP    : each successful write/exec tool extends runway
#                        by STEP_PROGRESS_GRACE (capped at hard cap)
#   STEP_HARD_CAP      : in default mode, prompt user to continue;
#                        in auto/bypass, auto-extend ONCE on progress
#                        then truly terminate; in plan, terminate.
# Backward compat: JANUS_MAX_STEPS still accepted as alias for
# STEP_HARD_CAP (legacy single-knob), and if only that is set the soft
# cap defaults to MAX_STEPS // 2.
_LEGACY_MAX_STEPS_ENV = os.getenv("JANUS_MAX_STEPS")
STEP_HARD_CAP: int = int(
    os.getenv("JANUS_STEP_HARD_CAP", _LEGACY_MAX_STEPS_ENV or "200")
)
STEP_SOFT_CAP: int = int(
    os.getenv(
        "JANUS_STEP_SOFT_CAP",
        str(int(_LEGACY_MAX_STEPS_ENV) // 2)
        if _LEGACY_MAX_STEPS_ENV
        else "50",
    )
)
STEP_PROGRESS_GRACE: int = int(os.getenv("JANUS_STEP_PROGRESS_GRACE", "20"))
# MAX_STEPS retained as backward-compat alias. Old code reading it gets
# the hard cap, which preserves the "absolute ceiling" semantics it
# always had (just at a higher default).
MAX_STEPS: int = STEP_HARD_CAP
LLM_TIMEOUT: int = int(os.getenv("JANUS_LLM_TIMEOUT", "180"))
# Hard cap on shell tool timeout so the model can't hang the agent for
# hours by passing timeout=600000 (a v1.1 incident: model thought the
# value was milliseconds, sent 600s × 1000, subprocess.run blocked
# 166 hours on a daemon that never exits). 300s = 5 min.
SHELL_TIMEOUT_MAX: int = int(os.getenv("JANUS_SHELL_TIMEOUT_MAX", "300"))

# --- Approval policy ---
APPROVAL_MODE: str = os.getenv("JANUS_APPROVAL", "manual")

# --- Phase 2: memory ---
MEMORY_PROPOSE_MODEL: str = os.getenv("JANUS_MEMORY_MODEL", "")
# v1.35.0 — Phase 9.4: multi-model routing per purpose. Each env var
# falls back to MODEL when unset, so existing setups stay on the
# main model unchanged. Cost dashboard splits per-purpose so the
# user can see if a cheaper memory/verify/subagent model is paying
# off.
VERIFY_MODEL: str = os.getenv("JANUS_VERIFY_MODEL", "")
SUBAGENT_MODEL: str = os.getenv("JANUS_SUBAGENT_MODEL", "")
TITLE_MODEL: str = os.getenv("JANUS_TITLE_MODEL", "")
# v1.37.1 — Phase 10.1.1: /goal Ralph Loop judge model. Cheap by
# design — the judge is called once per turn while a goal is
# active, so a 500-turn budget × main-model cost would dwarf the
# value. Recommended values: anthropic/claude-haiku-4-5 or
# openai/gpt-4o-mini. Falls back to MODEL when unset; users on
# tight budgets should set this explicitly.
JUDGE_MODEL: str = os.getenv("JANUS_JUDGE_MODEL", "")


def model_for_purpose(purpose: str) -> str:
    """v1.35.0 — return the configured model for a given purpose,
    falling back to the main MODEL when no override is set.

    Purposes:
      'chat'      — main turn (== MODEL always)
      'memory'    — memory diff propose loop (cheap default OK)
      'verify'    — post-edit pytest output classification
      'subagent'  — spawned subagent leaf
      'title'     — conversation title generation
      'judge'     — /goal Ralph Loop achievement evaluator (v1.37.1;
                    cheap recommended)
      anything else → MODEL
    """
    table = {
        "memory": MEMORY_PROPOSE_MODEL,
        "verify": VERIFY_MODEL,
        "subagent": SUBAGENT_MODEL,
        "title": TITLE_MODEL,
        "judge": JUDGE_MODEL,
    }
    override = table.get(purpose, "")
    return override or MODEL
MEMORY_PREPEND_BYTES: int = int(os.getenv("JANUS_MEMORY_BYTES", "4096"))
MEMORY_PROPOSE_ENABLED: bool = os.getenv("JANUS_MEMORY_PROPOSE", "1") not in ("0", "false", "no")

# v1.18: structured memory cards live alongside the legacy 5 .md files.
# Each card is a single markdown file with typed frontmatter; SQLite at
# index.db is a derived cache (rebuildable from cards/). P5 holds: cards
# remain plain-text canonical.
MEMORY_CARDS_DIR: Path = MEMORY_DIR / "cards"
MEMORY_INDEX_DB: Path = MEMORY_DIR / "index.db"
MEMORY_RECALLS_LOG: Path = MEMORY_DIR / "recalls.jsonl"
MEMORY_RECALL_TOP_K: int = int(os.getenv("JANUS_MEMORY_RECALL_TOP_K", "5"))
MEMORY_RECALL_BUDGET_BYTES: int = int(
    os.getenv("JANUS_MEMORY_RECALL_BUDGET", "900")
)
# Auto-pruning thresholds (Phase 8). Pure compute; no LLM call.
MEMORY_AUTO_PRUNE: bool = os.getenv("JANUS_MEMORY_AUTO_PRUNE", "1") not in ("0", "false", "no")
MEMORY_PRUNE_ACTIVE_DAYS: int = int(os.getenv("JANUS_MEMORY_PRUNE_ACTIVE_DAYS", "21"))
MEMORY_PRUNE_LOWCONF_DAYS: int = int(os.getenv("JANUS_MEMORY_PRUNE_LOWCONF_DAYS", "120"))
MEMORY_PRUNE_LOWCONF_THRESHOLD: float = float(
    os.getenv("JANUS_MEMORY_PRUNE_LOWCONF", "0.4")
)
MEMORY_PRUNE_SUPERSEDED_DAYS: int = int(
    os.getenv("JANUS_MEMORY_PRUNE_SUPERSEDED_DAYS", "30")
)
MEMORY_PROTECTED_DURABILITY: float = float(
    os.getenv("JANUS_MEMORY_PROTECTED_DURABILITY", "0.7")
)

# v1.43.0 — daemon-managed periodic prune cadence. Both memory_prune
# and skill_prune are pure-compute (no LLM cost), so default-on with
# a 24h cadence is safe. Disable individually with ``=0``.
MEMORY_PRUNE_HOURS: int = int(os.getenv("JANUS_MEMORY_PRUNE_HOURS", "24"))
SKILL_PRUNE_HOURS: int = int(os.getenv("JANUS_SKILL_PRUNE_HOURS", "24"))

# v1.43.0 — skill_prune thresholds.
# Quarantined skills not promoted within this window AND with no runs
# get moved to ``SKILLS_DIR/_trash/``. Reversible by `mv` back.
SKILL_PRUNE_QUARANTINE_DAYS: int = int(
    os.getenv("JANUS_SKILL_PRUNE_QUARANTINE_DAYS", "30")
)
# Trashed skills older than this get permanently unlinked.
SKILL_PRUNE_TRASH_DAYS: int = int(
    os.getenv("JANUS_SKILL_PRUNE_TRASH_DAYS", "30")
)
# Trusted (promoted) skills inactive for this many days get a
# ``stale_warning`` frontmatter flag — never auto-deleted/demoted.
SKILL_STALE_DAYS: int = int(os.getenv("JANUS_SKILL_STALE_DAYS", "60"))

# v1.44.0 — GEPA evolutionary engine tunables.
# Defaults give ~200 LLM calls per run (pop=6 × records=10 × gen=3 plus
# mutations + baseline). Acceptable on Ollama Turbo / local. Crank
# JANUS_GEPA_MAX_LLM_CALLS down for paid endpoints.
GEPA_GENERATIONS: int = int(os.getenv("JANUS_GEPA_GENERATIONS", "3"))
GEPA_POPULATION: int = int(os.getenv("JANUS_GEPA_POPULATION", "6"))
GEPA_RECORDS_PER_RUN: int = int(os.getenv("JANUS_GEPA_RECORDS_PER_RUN", "10"))
GEPA_MAX_LLM_CALLS: int = int(os.getenv("JANUS_GEPA_MAX_LLM_CALLS", "250"))
# Minimum improvement (0-100 score points) over baseline before
# recommendation flips from "no_change" to "apply".
GEPA_PROMOTE_MARGIN: float = float(os.getenv("JANUS_GEPA_PROMOTE_MARGIN", "5.0"))

# v1.25.2 — single-user mode. When true, ``user_turn`` card extractions
# default to ``scope=global`` instead of the per-origin scope (telegram:
# <chat_id>, web:<session>, cli, etc.). The privacy invariant for
# tool_result extractions is UNCHANGED: those still scope-local because
# their content can be prompt-injected (web fetch, file reads, shell
# results). Default on — most Janus installs are one user across many
# surfaces, and the per-origin default surprised them by making CLI not
# see what Telegram had been saving for months. Set
# ``JANUS_SINGLE_USER=0`` for multi-user deployments where users on
# different chat_ids genuinely want isolated memory.
MEMORY_SINGLE_USER: bool = os.getenv("JANUS_SINGLE_USER", "1") not in (
    "0", "false", "no", "off",
)

# v1.3 — multi-category memory. Each category is a separate .md under
# MEMORY_DIR; the loader concatenates them in this order into the system
# prompt (earlier categories weigh more — soul first frames the agent's
# identity before anything else). Users can drop additional .md files
# in MEMORY_DIR; they're loaded after these in alpha order.
MEMORY_CATEGORIES: list[str] = [
    "soul",            # agent identity (name, role, tone, capabilities self-description)
    "user",            # who the user is (existing user.md content lives here after migration)
    "project",         # current workspace / project context (decays fast)
    "preferences",     # style, format, output preferences
    "relationships",   # other people in user's life — privacy-flagged
]

# --- Phase 2: eval ---
EVAL_DEFAULT_LAST: int = int(os.getenv("JANUS_EVAL_LAST", "20"))

# --- Phase 4: planner ---
PLAN_MAX_DEPTH: int = int(os.getenv("JANUS_PLAN_MAX_DEPTH", "3"))
PLAN_MAX_FANOUT: int = int(os.getenv("JANUS_PLAN_MAX_FANOUT", "6"))
PLAN_LEAF_STEPS: int = int(os.getenv("JANUS_PLAN_LEAF_STEPS", "15"))

# --- Phase 5: gateways ---
TELEGRAM_BOT_TOKEN: str = os.getenv("JANUS_TELEGRAM_TOKEN", "")
TELEGRAM_ALLOWED_CHATS: str = os.getenv("JANUS_TELEGRAM_CHATS", "")
# v1.31.16 — Hermes-style quiet telegram by default. Pre-v1.31.16 the
# gateway emitted a separate Telegram message for every tool_start /
# tool_end / skill_loaded / memory_update / thinking event. Sam's
# field-test compared this against Hermes (which emits nothing
# during the turn — only the model's final summary message lands)
# and found Janus's per-event spam noisy. Default is now QUIET; set
# JANUS_TELEGRAM_VERBOSE=1 to bring back the v1.31.x glyph stream.
TELEGRAM_VERBOSE: bool = os.getenv("JANUS_TELEGRAM_VERBOSE", "0").lower() in (
    "1", "true", "yes", "on",
)

# --- Phase 6: daemon ---
DAEMON_POLL_SECONDS: int = int(os.getenv("JANUS_DAEMON_POLL", "30"))
DAEMON_NOTIFY_GATEWAY: str = os.getenv("JANUS_DAEMON_GATEWAY", "log")

# v1.30.2 — built-in memory consolidation cadence inside the daemon.
# OFF by default (set to 0 hours = disabled) because consolidation
# burns LLM tokens; users opt in by setting a positive interval.
# Recommended: 24 (once per day). The original v1.18 design left
# this manual; v1.30.2 adds a daemon-managed cadence as an
# alternative to wiring agent_create() for the same purpose.
MEMORY_CONSOLIDATE_HOURS: int = int(
    os.getenv("JANUS_MEMORY_CONSOLIDATE_HOURS", "0")
)
# Multi-stage variant flag. Defaults to single-stage (v1.18 path);
# users wanting the v1.29.0 swarm-shaped pipeline opt in.
MEMORY_CONSOLIDATE_MULTI_STAGE: bool = (
    os.getenv("JANUS_MEMORY_CONSOLIDATE_MULTI_STAGE", "0") == "1"
)

# --- Phase 7: skill evolution ---
SKILL_REVIEW_EVERY: int = int(os.getenv("JANUS_SKILL_REVIEW_EVERY", "5"))
DEMO_DIR: Path = HOME / "demo"

# --- Phase 8: subagents ---
SUBAGENT_CONCURRENCY: int = int(os.getenv("JANUS_SUBAGENT_CONCURRENCY", "4"))
SUBAGENT_TIMEOUT: int = int(os.getenv("JANUS_SUBAGENT_TIMEOUT", "300"))
# JANUS_IS_SUBAGENT is set to "1" in spawned subagent env. The orchestrator
# checks this flag and refuses to spawn nested subagents (no recursion).
IS_SUBAGENT: bool = os.getenv("JANUS_IS_SUBAGENT") == "1"

# --- Phase 9: tool surface expansion ---
CODE_EXEC_TIMEOUT_DEFAULT: int = int(os.getenv("JANUS_CODE_EXEC_TIMEOUT", "10"))
CODE_EXEC_TIMEOUT_MAX: int = int(os.getenv("JANUS_CODE_EXEC_TIMEOUT_MAX", "30"))
CODE_EXEC_OUTPUT_BYTES: int = int(os.getenv("JANUS_CODE_EXEC_OUTPUT_BYTES", "50000"))
BRAVE_API_KEY: str = os.getenv("JANUS_BRAVE_API_KEY", "")
WEB_SEARCH_PROVIDER: str = os.getenv("JANUS_WEB_SEARCH", "brave")
TODOS_FILE: Path = HOME / "todos.json"
GREP_TIMEOUT: int = int(os.getenv("JANUS_GREP_TIMEOUT", "30"))
GREP_MAX_LINES: int = int(os.getenv("JANUS_GREP_MAX_LINES", "200"))
GLOB_MAX_RESULTS: int = int(os.getenv("JANUS_GLOB_MAX_RESULTS", "200"))
BROWSER_TIMEOUT: int = int(os.getenv("JANUS_BROWSER_TIMEOUT", "30"))

# --- Phase 10: MCP + skills market ---
MCP_DIR: Path = HOME / "mcp"
MCP_SERVERS_FILE: Path = MCP_DIR / "servers.json"
# Interop with Claude Code: read the shared settings file too.
CLAUDE_SETTINGS_FILE: Path = Path(
    os.getenv("CLAUDE_SETTINGS",
              str(Path.home() / ".claude" / "settings.json"))
)
MCP_INIT_TIMEOUT: int = int(os.getenv("JANUS_MCP_INIT_TIMEOUT", "10"))
MCP_CALL_TIMEOUT: int = int(os.getenv("JANUS_MCP_CALL_TIMEOUT", "30"))
SKILLS_MARKET_FETCH_TIMEOUT: int = int(os.getenv("JANUS_SKILLS_FETCH_TIMEOUT", "30"))

# --- Phase 11: hooks + gateways ---
HOOKS_FILE: Path = HOME / "hooks.json"
HOOKS_DIR: Path = HOME / "hooks"
HOOK_TIMEOUT: int = int(os.getenv("JANUS_HOOK_TIMEOUT", "30"))
WEB_HOST: str = os.getenv("JANUS_WEB_HOST", "127.0.0.1")
WEB_PORT: int = int(os.getenv("JANUS_WEB_PORT", "8765"))
WEB_HOST_OK: bool = os.getenv("JANUS_WEB_HOST_OK", "0") == "1"
WHATSAPP_TOKEN: str = os.getenv("JANUS_WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID: str = os.getenv("JANUS_WHATSAPP_PHONE_ID", "")
WHATSAPP_VERIFY_TOKEN: str = os.getenv("JANUS_WHATSAPP_VERIFY", "")
WHATSAPP_ALLOWED_NUMBERS: str = os.getenv("JANUS_WHATSAPP_ALLOWED", "")

# --- Phase 13: conversation continuity + cost ---
CONVERSATIONS_DIR: Path = HOME / "conversations"
CONVERSATION_RECAP_TURNS: int = int(os.getenv("JANUS_RECAP_TURNS", "5"))
COMPACT_THRESHOLD_TURNS: int = int(os.getenv("JANUS_COMPACT_THRESHOLD", "20"))
# JSON map of model_id → {input_per_million, output_per_million} in USD.
# Allows users to override the built-in price table for new models.
MODEL_PRICES_JSON: str = os.getenv("JANUS_MODEL_PRICES_JSON", "")

# --- Phase 15: customization ---
COMMANDS_DIR: Path = HOME / "commands"
OUTPUT_STYLE: str = os.getenv("JANUS_OUTPUT_STYLE", "markdown")

# --- Phase 20: provider niceties ---
# When set ('1'/'true'/'yes'), llm.chat wraps the system message in
# Anthropic-style content blocks with cache_control: ephemeral. Use this
# when JANUS_API_BASE proxies to Anthropic and you want their prompt
# cache to apply (OpenRouter honors the marker; OpenAI ignores it).
PROMPT_CACHE_MARKERS: bool = os.getenv("JANUS_PROMPT_CACHE", "0") in ("1", "true", "yes")

# v1.16.2: strip the `tools` payload from chat requests. Useful when
# the endpoint is a self-hosted vLLM that wasn't started with
# `--enable-auto-tool-choice` and 404s on tools-bearing requests, OR
# a model that doesn't support function calling. With NO_TOOLS=1 the
# agent can still chat but cannot call tools — degraded mode.
NO_TOOLS: bool = os.getenv("JANUS_NO_TOOLS", "0") in ("1", "true", "yes")

# --- v1.4: agent swarms ---
# Hard ceilings the spec validator enforces — a spec cannot request more
# than these regardless of what its `budget:` block says. Defense in depth.
SWARMS_DIR: Path = HOME / "swarms"
SWARM_SPECS_DIR: Path = SWARMS_DIR / "specs"
SWARM_RUNS_DIR: Path = SWARMS_DIR / "runs"
SWARM_MAX_SUBAGENTS: int = int(os.getenv("JANUS_SWARM_MAX_SUBAGENTS", "30"))
SWARM_MAX_BUDGET_USD: float = float(os.getenv("JANUS_SWARM_MAX_BUDGET_USD", "10"))
SWARM_MAX_WALLCLOCK_S: int = int(os.getenv("JANUS_SWARM_MAX_WALLCLOCK_S", "1800"))
SWARM_MAX_RECURSION_DEPTH: int = int(os.getenv("JANUS_SWARM_MAX_RECURSION_DEPTH", "2"))
SWARM_DEFAULT_CONCURRENCY: int = int(os.getenv("JANUS_SWARM_CONCURRENCY", "5"))
SWARM_MAX_COMPLETION_TOKENS_PER_ROLE: int = int(
    os.getenv("JANUS_SWARM_MAX_COMPLETION_TOKENS", "800")
)

# v1.4: retry/backoff at the llm.chat boundary. A long-running swarm
# (12hr unattended runs Sam wants) will hit transient HTTP 5xx and
# ConnectionError; without retries it dies on the first hiccup. Bounded
# by max attempts (the call counts as attempt 1; default 3 = 1 try + 2
# retries). Backoff is exponential with jitter: base * 2^attempt + U(0, base).
LLM_RETRY_MAX_ATTEMPTS: int = int(os.getenv("JANUS_LLM_RETRY_MAX_ATTEMPTS", "3"))
LLM_RETRY_BACKOFF_BASE_S: float = float(os.getenv("JANUS_LLM_RETRY_BACKOFF_BASE", "2.0"))


def ensure_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    TRIGGERS_DIR.mkdir(parents=True, exist_ok=True)
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    SWARM_SPECS_DIR.mkdir(parents=True, exist_ok=True)
    SWARM_RUNS_DIR.mkdir(parents=True, exist_ok=True)


def memory_model() -> str:
    return MEMORY_PROPOSE_MODEL or MODEL


def assert_configured() -> None:
    if not API_KEY:
        raise SystemExit(
            "error: JANUS_API_KEY not set.\n"
            "  set JANUS_API_KEY, JANUS_API_BASE, JANUS_MODEL.\n"
        )
