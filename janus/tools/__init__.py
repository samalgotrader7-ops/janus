"""tools/__init__.py — bundled tool list."""

from .base import (
    Tool, Registry, make_capability_aware, make_auto_aware, make_protected,
)
from .capabilities import Capability, CapabilitySet
from .fs import FsRead, FsWrite, FsList
from .shell import Shell
from .web import WebFetch
# Phase 9 tools.
from .edit import FsEdit
from .multi_edit import FsMultiEdit
from .glob import FsGlob
from .grep import FsGrep
from .todo import TodoRead, TodoWrite
from .session_search import SessionSearch, SessionRecent
from .code_exec import CodeExecPython
from .notebook import NbRead, NbEdit
from .web_search import WebSearch
from .browser import (
    BrowserNavigate, BrowserText, BrowserSnapshot,
    BrowserLinks, BrowserGetImage,
)
from .vision import ImageDescribe
from .swarm_run import SwarmRun
from .telegram_send import TelegramSendFile, TelegramSendMessage
from .telegram_react import TelegramReact
from .interview_ask import InterviewAsk
from .agent import (
    AgentCreate, AgentList, AgentRunNow, AgentDelete, AgentSetEnabled,
)
from .clarify import Clarify
from .delegate import Delegate
from .subagent import Subagent
from .ssh_exec import SshExec
from .shell_bg import ShellRunBg, ShellOutput, ShellKill, ShellList
from .plan_mode import ExitPlanMode
from .memory_search import MemorySearch
# v1.34.2 — image generation (Phase 7.3).
from .image_gen import ImageGen
# v1.38.0 — external CLI agent wrappers (Phase 10.2.0). First entry:
# Claude Code via its non-interactive `-p` Print Mode.
from .claude_code import ClaudeCode
# v1.38.1 — Aider via `aider --message` (Phase 10.2.1).
from .aider import Aider
# v1.38.2 — OpenAI Codex CLI via `codex exec` (Phase 10.2.2).
from .codex_cli import CodexCli
# v1.38.3 — Google Gemini CLI via `gemini -p` (Phase 10.2.3).
from .gemini_cli import GeminiCli


_BUILTIN_TOOL_FACTORIES = {
    # Phases 1-3
    "fs_read": FsRead,
    "fs_list": FsList,
    "fs_write": FsWrite,
    "shell": Shell,
    "web_fetch": WebFetch,
    # Phase 9 — filesystem
    "fs_edit": FsEdit,
    "fs_multi_edit": FsMultiEdit,
    "fs_glob": FsGlob,
    "fs_grep": FsGrep,
    # Phase 9 — agent state / introspection
    "todo_read": TodoRead,
    "todo_write": TodoWrite,
    "session_search": SessionSearch,
    "session_recent": SessionRecent,
    # Phase 9 — code/notebook
    "code_exec_python": CodeExecPython,
    "nb_read": NbRead,
    "nb_edit": NbEdit,
    # Phase 9 — web/browser/vision
    "web_search": WebSearch,
    "browser_navigate": BrowserNavigate,
    "browser_text": BrowserText,
    "browser_snapshot": BrowserSnapshot,
    "browser_links": BrowserLinks,
    "browser_get_image": BrowserGetImage,
    "image_describe": ImageDescribe,
    # v1.5 — model-callable swarm spawn (recursion guard already in place).
    "swarm_run": SwarmRun,
    # v1.5.2 — direct Telegram Bot API tools so the model can send files /
    # messages from CLI / headless / sub-agent contexts (not just from
    # inside the Telegram gateway). Need JANUS_TELEGRAM_TOKEN set.
    "telegram_send_file": TelegramSendFile,
    "telegram_send_message": TelegramSendMessage,
    # v1.18.2 — friendliness on Telegram. Model can react to the user's
    # most-recent message with an emoji. Gateway-bound: outside the
    # Telegram chat loop, returns "not on Telegram" gracefully.
    "telegram_react": TelegramReact,
    # v1.19.0 Phase 5 — model-callable contextual interview question.
    # Pulls the next eligible question from the bundled library and
    # asks via the gateway's clarify UI. Gateway-bound (callback-less
    # outside a chat); replaced per-turn in cli_rich/telegram with a
    # callback-bearing instance.
    "interview_ask": InterviewAsk,
    # v1.6.0 — scheduled-agent lifecycle. THE LIFETIME-SOLUTION TOOLS.
    # Before these, the model would lie about creating agents (just
    # writing to memory) because it had no real machinery. Now it
    # creates a skill+trigger pair and the daemon fires it on schedule.
    "agent_create": AgentCreate,
    "agent_list": AgentList,
    "agent_run_now": AgentRunNow,
    "agent_delete": AgentDelete,
    "agent_set_enabled": AgentSetEnabled,
    # v1.8.0 — Tier A item 2 (Hermes parity).
    # `clarify` lets the model ask the user one question mid-turn.
    # Constructor takes a callback the gateway/CLI injects; the bundled
    # default is the no-callback variant (returns "[clarify unavailable]")
    # so headless / sub-agent contexts don't crash. The chat surfaces
    # (cli_rich, telegram) override the registration with a callback-
    # bearing instance so the user actually sees the prompt.
    "clarify": Clarify,
    # `delegate` spawns a fresh executor.chat for one focused sub-task.
    # Restricted tool surface (read-only by default), bounded steps,
    # recursion blocked at depth 1. Lighter than swarm_run.
    # DEPRECATED in v1.27.0 — use `subagent` instead. Kept bundled for
    # back-compat; will be removed in v1.28.x.
    "delegate": Delegate,
    # v1.27.0 — first-class subagent spawn. Replaces delegate with
    # structured briefing (description + prompt), subagent_type
    # presets (general / explore / plan / code-review), live progress
    # forwarding to the parent's event stream, and audit logging.
    "subagent": Subagent,
    # v1.11.0 — remote command execution via system ssh. BatchMode=yes
    # (key auth only, no password prompts). Reuses ~/.ssh/config aliases,
    # ProxyJump, agent forwarding. Capability tokens: ssh.exec.
    "ssh_exec": SshExec,
    # v1.15.0 — coding-agent gap fillers (Claude Code parity).
    # Background shell: launch + monitor + kill long-running processes
    # without blocking the chat loop. State machine via shell_id.
    "shell_run_bg": ShellRunBg,
    "shell_output": ShellOutput,
    "shell_kill": ShellKill,
    "shell_list": ShellList,
    # ExitPlanMode: model-callable "I have a plan, can I proceed?"
    # Only meaningful in mode=plan. Approver shows plan to user;
    # framework switches mode on approval.
    "exit_plan_mode": ExitPlanMode,
    # v1.18.0 — structured memory search. Lets the model query the
    # FTS5-backed card store mid-turn (complements pre-injection recall).
    # Restricts to current origin scope unless caller passes scope=global.
    "memory_search": MemorySearch,
    # v1.34.2 — image generation (Phase 7.3). Wraps DALL-E (default) or
    # other OpenAI-compatible image providers via JANUS_IMAGE_PROVIDER.
    # risk='exec' — costs money + writes files to ~/.janus/images/.
    "image_gen": ImageGen,
    # v1.38.0 — Phase 10.2.0: External CLI agent wrappers. Hands a
    # focused sub-task off to Anthropic's Claude Code via `claude -p`
    # Print Mode. risk='exec' (can edit files in cwd); capability
    # token external_cli.claude_code.exec. Future v10.2.x adds
    # aider, codex_cli, gemini_cli on the same shape.
    "claude_code": ClaudeCode,
    # v1.38.1 — Phase 10.2.1: Aider (https://aider.chat) via
    # `aider --message --yes-always`. Pair-programming agent that
    # auto-commits to git. Capability token external_cli.aider.exec.
    "aider": Aider,
    # v1.38.2 — Phase 10.2.2: OpenAI Codex CLI
    # (https://github.com/openai/codex) via `codex exec`. Supports
    # extra_args for --json structured output / --model overrides.
    # JANUS_CODEX_BIN + JANUS_CODEX_FLAGS env overrides.
    "codex_cli": CodexCli,
    # v1.38.3 — Phase 10.2.3: Google Gemini CLI
    # (https://github.com/google-gemini/gemini-cli) via `gemini -p`.
    # Supports extra_args for --all-files / --model / --sandbox.
    # JANUS_GEMINI_BIN + JANUS_GEMINI_FLAGS env overrides.
    "gemini_cli": GeminiCli,
}


def default_registry(
    capabilities: CapabilitySet | None = None,
    *,
    tool_names: list[str] | None = None,
) -> Registry:
    """Bundled tool set.

    `capabilities`: active skill's grants (Phase 3) — empty = every dangerous
        action prompts.
    `tool_names`: subset of bundled names to expose (Phase 8 — subagents
        receive a restricted registry). Unknown names are silently dropped;
        passing an empty list yields a registry with zero tools (which is
        valid — a leaf may be a pure-reasoning task).
    """
    if tool_names is None:
        selected = list(_BUILTIN_TOOL_FACTORIES.values())
    else:
        selected = [
            _BUILTIN_TOOL_FACTORIES[n]
            for n in tool_names
            if n in _BUILTIN_TOOL_FACTORIES
        ]
    reg = Registry([cls() for cls in selected], capabilities=capabilities)
    # Phase 10: mount any active MCP clients into this registry. Best-effort —
    # if MCP module can't import or a client is broken, the rest of the
    # registry still works.
    try:
        from ..mcp.client import get_active_clients, mount_mcp_tools
        for server_name, client in get_active_clients().items():
            try:
                mount_mcp_tools(reg, server_name, client)
            except Exception:
                continue
    except Exception:
        pass
    return reg


__all__ = [
    "Tool",
    "Registry",
    "Capability",
    "CapabilitySet",
    "default_registry",
    "make_capability_aware",
]
