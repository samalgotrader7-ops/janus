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
