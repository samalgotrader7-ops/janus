"""
diff.py — render unified diffs for the approver UI (Phase 14).

WHY:
Today the approver prompt for `fs_write` shows "size N bytes". For
`fs_edit` it shows the occurrence count. Neither tells the user WHAT the
change does. A `git diff --unified=3` style view is the obvious answer
and lets the user catch wrong edits before they touch the disk.

NO DEPS:
Uses stdlib `difflib`. Coloring is done with raw ANSI escape codes so
both the basic CLI and the rich CLI can use the same renderer.
"""

from __future__ import annotations
import difflib


# ANSI colors. Same palette as cli.py's `C` class so the diff blends in.
_GREEN = "\033[32m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
_R = "\033[0m"


def render(
    old: str,
    new: str,
    *,
    path: str = "",
    context: int = 3,
    color: bool = True,
    max_lines: int = 200,
) -> str:
    """Return a unified-diff string. Empty string if old == new.

    `path`: shown in the file header (e.g. "src/main.py").
    `context`: lines of context around each hunk (mirrors `git diff -U3`).
    `color`: True wraps + lines green and - lines red with ANSI.
    `max_lines`: hard cap; longer diffs get a truncation marker.
    """
    if old == new:
        return ""

    old_lines = old.splitlines(keepends=False)
    new_lines = new.splitlines(keepends=False)

    label_old = f"a/{path}" if path else "a/file"
    label_new = f"b/{path}" if path else "b/file"

    diff_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=label_old,
        tofile=label_new,
        n=context,
        lineterm="",
    ))

    if not diff_lines:
        return ""

    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines] + [
            f"... [{len(diff_lines) - max_lines} more lines truncated]"
        ]

    if not color:
        return "\n".join(diff_lines)

    colored: list[str] = []
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            colored.append(f"{_DIM}{line}{_R}")
        elif line.startswith("@@"):
            colored.append(f"{_CYAN}{line}{_R}")
        elif line.startswith("+"):
            colored.append(f"{_GREEN}{line}{_R}")
        elif line.startswith("-"):
            colored.append(f"{_RED}{line}{_R}")
        else:
            colored.append(line)
    return "\n".join(colored)


def render_rich(
    old: str,
    new: str,
    *,
    path: str = "",
    context: int = 3,
    max_lines: int = 200,
):
    """v1.25.4: return a Rich Syntax object for the unified diff so
    approval prompts in cli_rich render with proper line numbers and
    diff syntax highlighting (red for removed, green for added).

    Returns None when:
      * Rich isn't importable (basic CLI / headless environments)
      * old == new (no diff to render)

    Callers should fall back to ``render()`` (the ANSI version) when
    None is returned.

    The plain ANSI version is still used by:
      - cli.py basic surface
      - logs that strip color via JANUS_NO_COLOR
      - any non-Rich consumer
    """
    try:
        from rich.syntax import Syntax
    except ImportError:
        return None
    if old == new:
        return None
    plain = render(
        old, new,
        path=path, context=context, color=False, max_lines=max_lines,
    )
    if not plain:
        return None
    return Syntax(
        plain, "diff",
        theme="ansi_dark",
        line_numbers=True,
        word_wrap=False,
        background_color="default",  # don't paint a fill — terminal bg
    )


def stat(old: str, new: str) -> str:
    """One-line `+N -M` summary of the change."""
    if old == new:
        return "no change"
    sm = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines())
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
        elif tag == "replace":
            removed += i2 - i1
            added += j2 - j1
    return f"+{added} -{removed}"
