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
USER_MODEL_FILE: Path = HOME / "user.md"
SKILLS_DIR: Path = HOME / "skills"
EVALS_DIR: Path = HOME / "evals"
TRIGGERS_DIR: Path = HOME / "triggers"
DAEMON_STATE: Path = HOME / "daemon.state.json"
HISTORY_FILE: Path = HOME / "cli_history"

# --- Workspace ---
WORKSPACE: Path = Path(os.getenv("JANUS_WORKSPACE", str(Path.cwd()))).resolve()

# --- Loop limits ---
MAX_STEPS: int = int(os.getenv("JANUS_MAX_STEPS", "25"))
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
MEMORY_PREPEND_BYTES: int = int(os.getenv("JANUS_MEMORY_BYTES", "4096"))
MEMORY_PROPOSE_ENABLED: bool = os.getenv("JANUS_MEMORY_PROPOSE", "1") not in ("0", "false", "no")

# --- Phase 2: eval ---
EVAL_DEFAULT_LAST: int = int(os.getenv("JANUS_EVAL_LAST", "20"))

# --- Phase 4: planner ---
PLAN_MAX_DEPTH: int = int(os.getenv("JANUS_PLAN_MAX_DEPTH", "3"))
PLAN_MAX_FANOUT: int = int(os.getenv("JANUS_PLAN_MAX_FANOUT", "6"))
PLAN_LEAF_STEPS: int = int(os.getenv("JANUS_PLAN_LEAF_STEPS", "15"))

# --- Phase 5: gateways ---
TELEGRAM_BOT_TOKEN: str = os.getenv("JANUS_TELEGRAM_TOKEN", "")
TELEGRAM_ALLOWED_CHATS: str = os.getenv("JANUS_TELEGRAM_CHATS", "")

# --- Phase 6: daemon ---
DAEMON_POLL_SECONDS: int = int(os.getenv("JANUS_DAEMON_POLL", "30"))
DAEMON_NOTIFY_GATEWAY: str = os.getenv("JANUS_DAEMON_GATEWAY", "log")

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


def ensure_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    TRIGGERS_DIR.mkdir(parents=True, exist_ok=True)
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)


def memory_model() -> str:
    return MEMORY_PROPOSE_MODEL or MODEL


def assert_configured() -> None:
    if not API_KEY:
        raise SystemExit(
            "error: JANUS_API_KEY not set.\n"
            "  set JANUS_API_KEY, JANUS_API_BASE, JANUS_MODEL.\n"
        )
