"""
at_mentions.py — `@path` file references in user input (v1.25.1).

When the user types ``@path/to/file.py`` in their prompt, the user
input gets the file's contents inlined before reaching the model:

    User typed:    "what does @src/foo.py do?"
    Model sees:    "what does [file: src/foo.py] do?
                    ```
                    <file contents>
                    ```"

This is the Claude-Code-shape file-mention feature. Tab completion
in cli_rich uses ``AtPathCompleter`` to fuzzy-find workspace files;
this module handles the post-submit text expansion.

WHY POST-PROCESS THE TEXT (not auto-inject as a tool call):
- One round-trip — the model sees the file in its first read, no
  fs_read call needed (rule 22 says don't spelunk source for
  explanation questions; @ mentions ARE the explanation pattern).
- Cheap to implement: pure string transformation on the user_input
  before it hits executor.chat.
- Auditable: the user's own message in the conversation log shows
  what they referenced, with full contents.

DESIGN — CONSERVATIVE EXPANSION:
- Only @-tokens preceded by start-of-string or whitespace expand.
  ``user@domain.com`` does NOT match (no whitespace before @).
- Path resolves against ``workspace`` via ``security.resolve_within``.
  Out-of-workspace paths leave the @-token literal — security
  invariant matches fs_read.
- Binary files refuse (UTF-8 decode of first 4 KB must succeed).
- Size capped (default 50 KB per file). Bigger files truncate with
  a marker instead of refusing — better partial than nothing.
- Non-existent paths leave the @-token literal so the model sees the
  user's typo and can ask, rather than failing silently.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config, security


# Match `@<path>` where @ is at start-of-string or after whitespace.
# The path captures word chars, slashes, dots, dashes, underscores —
# the typical Unix path shape. Stops at whitespace.
_AT_TOKEN = re.compile(r"(^|\s)@([\w./_\-]+)")

# Per-file size cap. Files bigger than this truncate with a marker.
DEFAULT_MAX_BYTES = 50_000

# Heuristic: how many leading bytes to test for UTF-8-ness when
# deciding "is this a text file we can inline".
_BINARY_PROBE_BYTES = 4096


def _try_read_text(p: Path, max_bytes: int) -> tuple[str | None, str]:
    """Read ``p`` as text, returning (contents, status) where status is:
    ``ok`` | ``truncated`` | ``binary`` | ``too_big_skipped`` | ``unreadable``.

    Truncation: when the file is larger than ``max_bytes``, return the
    first ``max_bytes`` followed by a clear marker. Better partial
    than nothing for big files (logs, generated source).
    """
    try:
        size = p.stat().st_size
    except OSError:
        return None, "unreadable"
    # Hard ceiling: 5x max_bytes — bigger than this is almost certainly
    # not what the user meant to inline. Refuse rather than truncate.
    if size > max_bytes * 5:
        return None, "too_big_skipped"
    try:
        with p.open("rb") as fh:
            head = fh.read(_BINARY_PROBE_BYTES)
    except OSError:
        return None, "unreadable"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return None, "binary"
    try:
        # Re-read full bytes and decode. We already verified the head
        # is utf-8; tolerate decode errors on the tail by replacing —
        # better than refusing on a single bad byte deep in the file.
        data = p.read_bytes()
    except OSError:
        return None, "unreadable"
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace"), "ok"
    truncated = data[:max_bytes].decode("utf-8", errors="replace")
    truncated += (
        f"\n\n[... truncated: file is {len(data)} bytes, "
        f"showed first {max_bytes} ...]"
    )
    return truncated, "truncated"


def _resolve(rel: str, workspace: Path) -> Path | None:
    """Resolve ``rel`` against ``workspace`` via security.resolve_within.

    Returns None on out-of-workspace, missing, or non-file targets.
    """
    try:
        p = security.resolve_within(workspace, rel)
    except (ValueError, OSError):
        return None
    if not p.exists() or not p.is_file():
        return None
    return p


def expand_at_mentions(
    text: str,
    *,
    workspace: Path | str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[str, list[dict]]:
    """Expand ``@path`` tokens in ``text`` to inline file contents.

    Returns ``(expanded_text, log)`` where log is one dict per matched
    token::

        {"token": "@src/foo.py", "path": "src/foo.py",
         "status": "ok"|"truncated"|"binary"|"too_big_skipped"|
                   "unreadable"|"missing"}

    The log lets the surface (cli_rich) print a one-line summary so
    the user sees what got injected (or why a token was left literal).

    Tokens that fail expansion are left as-is in the text. We never
    raise — at-mention failure is observable but never blocks the
    chat turn.
    """
    if not text or "@" not in text:
        return text, []

    ws = Path(workspace) if workspace is not None else config.WORKSPACE
    log: list[dict] = []

    def _replace(m: re.Match) -> str:
        prefix, raw_path = m.group(1), m.group(2)
        rel = raw_path.rstrip(".,;:!?)\"'")  # strip common trailing punctuation
        # Reattach what we stripped at the end so sentences like
        # "look at @foo.py." keep the period.
        trailing = raw_path[len(rel):]
        target = _resolve(rel, ws)
        if target is None:
            log.append({
                "token": "@" + raw_path, "path": rel, "status": "missing",
            })
            return m.group(0)  # leave token literal
        contents, status = _try_read_text(target, max_bytes)
        if contents is None:
            log.append({
                "token": "@" + raw_path, "path": rel, "status": status,
            })
            return m.group(0)
        log.append({
            "token": "@" + raw_path, "path": rel, "status": status,
        })
        # Render with a fenced block so the model sees clear file
        # boundaries. Match Claude Code's mention shape.
        block = (
            f"{prefix}[file: {rel}]\n```\n{contents}\n```{trailing}"
        )
        return block

    expanded = _AT_TOKEN.sub(_replace, text)
    return expanded, log


# ---------- Completer support ----------


def list_workspace_files(
    workspace: Path | str | None = None,
    *,
    prefix: str = "",
    max_results: int = 50,
) -> list[str]:
    """Return up to ``max_results`` workspace-relative paths matching
    ``prefix``. Used by AtPathCompleter in cli_rich for Tab completion.

    Skips dot-directories (``.git``, ``.venv``, ``node_modules``,
    ``__pycache__``) by default since the user almost never @-references
    those. Sorted: directories first (with trailing slash), then files,
    each group alphabetical.
    """
    ws = Path(workspace) if workspace is not None else config.WORKSPACE
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__",
                 ".tox", ".mypy_cache", ".pytest_cache"}

    # Decide search root and remaining prefix from the typed value.
    # `@src/foo` → root = ws/src, remainder = "foo"
    # `@src/`    → root = ws/src, remainder = ""
    # `@foo`     → root = ws,     remainder = "foo"
    if "/" in prefix or os.sep in prefix:
        head, tail = os.path.split(prefix)
        try:
            root = (ws / head).resolve()
        except OSError:
            return []
        # Stay within workspace.
        try:
            root.relative_to(ws.resolve())
        except ValueError:
            return []
        rel_prefix = head + ("/" if head and not head.endswith("/") else "")
        remainder = tail
    else:
        root = ws
        rel_prefix = ""
        remainder = prefix

    if not root.exists() or not root.is_dir():
        return []

    out: list[tuple[int, str]] = []  # (sort_key_group, full_relpath)
    try:
        for entry in os.scandir(root):
            if entry.name.startswith(".") and entry.name not in (".", ".."):
                # Allow a literal `@.foo` if user typed `@.` deliberately;
                # otherwise hide dotfiles.
                if not remainder.startswith("."):
                    continue
            if entry.name in skip_dirs:
                continue
            if remainder and not entry.name.startswith(remainder):
                continue
            relpath = rel_prefix + entry.name
            if entry.is_dir():
                out.append((0, relpath + "/"))
            else:
                out.append((1, relpath))
            if len(out) >= max_results * 2:
                break
    except OSError:
        return []

    out.sort()
    return [p for _, p in out[:max_results]]
