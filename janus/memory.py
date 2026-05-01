"""
memory.py — persistent user model in plain markdown.

DESIGN NOTE:
The user model is a markdown file the *user owns*. We never silently rewrite
it. Every change is proposed as a diff and approved (y/N/edit).

Why markdown, not JSON or SQLite?
  - The user reads it. They edit it directly when they disagree.
  - It's a prompt fragment by construction — no serialization layer to
    debug when the LLM stops respecting it.
  - It diffs cleanly in git.

Sections are H2 (`## Identity`, `## Preferences`, ...). Order matters: earlier
sections are read first by the interpreter prompt and weigh more.

PROPOSE-AND-APPROVE FLOW:
After each interaction, cli.py calls propose_diff(). Janus emits a list of
{op, section, text} ops. CLI shows them; user approves or edits. Atomic write
on approve (write-to-tmp + os.replace).
"""

from __future__ import annotations
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from . import config, llm


# ---------- Public types ----------


class Op(TypedDict):
    op: str           # "append" | "replace" | "delete" | "create_section"
    section: str      # H2 section name (without leading "## ")
    text: str         # body text (ignored for "delete")


# ---------- Read ----------


def read() -> str:
    """Return full user.md text, or '' if missing."""
    if not config.USER_MODEL_FILE.exists():
        return ""
    return config.USER_MODEL_FILE.read_text(encoding="utf-8")


def read_section(name: str) -> str | None:
    """Return body of a section (between its H2 and the next H2/EOF), or None."""
    txt = read()
    if not txt:
        return None
    sections = parse_sections(txt)
    return sections.get(name)


def parse_sections(text: str) -> dict[str, str]:
    """Split markdown into {section_name: body}. H1 is treated as preamble (key '')."""
    sections: dict[str, str] = {}
    current = ""  # preamble before first H2
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    sections[current] = "\n".join(buf).strip()
    return sections


def render_sections(sections: dict[str, str]) -> str:
    """Inverse of parse_sections. Preserves preamble-first order if present."""
    parts: list[str] = []
    if "" in sections and sections[""]:
        parts.append(sections[""])
    for name, body in sections.items():
        if name == "":
            continue
        parts.append(f"## {name}")
        if body:
            parts.append(body)
    return "\n\n".join(parts).rstrip() + "\n"


def prepend_for_prompt() -> str:
    """Return the chunk of user.md to prepend to interpreter prompts.

    Returns '' if memory is empty. Truncated to MEMORY_PREPEND_BYTES so we
    don't blow the context budget on a long user.md.
    """
    txt = read()
    if not txt:
        return ""
    n = config.MEMORY_PREPEND_BYTES
    if len(txt) <= n:
        body = txt
    else:
        body = txt[:n] + "\n[user.md truncated for prompt]"
    return f"# Known about the user (from ~/.janus/user.md)\n\n{body}\n\n---\n"


# ---------- Apply ----------


def apply(ops: list[Op]) -> None:
    """Apply a list of ops to user.md atomically."""
    sections = parse_sections(read())
    for op in ops:
        kind = op.get("op", "")
        section = op.get("section", "").strip()
        text = (op.get("text") or "").strip()
        if not section:
            continue
        if kind == "create_section":
            sections.setdefault(section, text)
        elif kind == "append":
            existing = sections.get(section, "")
            sections[section] = (existing + "\n" + text).strip() if existing else text
        elif kind == "replace":
            sections[section] = text
        elif kind == "delete":
            sections.pop(section, None)
        # silent on unknown ops — the propose step is the validation point

    rendered = render_sections(sections)
    if "" not in sections or not sections[""]:
        rendered = "# user.md\n\n" + rendered
    _atomic_write(config.USER_MODEL_FILE, rendered)


def _atomic_write(path: Path, content: str) -> None:
    config.ensure_home()
    fd, tmp = tempfile.mkstemp(prefix=".user.md.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- Propose (LLM) ----------


PROPOSE_SYSTEM = """You maintain a persistent user model in plain markdown.

You will receive:
  - the user's request,
  - the agent's final output,
  - the current user.md (may be empty).

Decide if anything DURABLE about the user was revealed. Durable means it would
plausibly matter again in a future, unrelated conversation: their identity,
projects, tools, preferences, recurring constraints. Do NOT record one-off facts
("they asked about X today"), session noise, or content the user could equally
get from public docs.

Be conservative. Most turns produce ZERO ops. Empty list is the right answer
unless something genuinely new and durable showed up.

Return STRICT JSON:
{
  "ops": [
    {"op": "append" | "replace" | "create_section" | "delete",
     "section": "<H2 name>",
     "text": "<markdown body, or empty for delete>"}
  ]
}

No prose, no markdown fences, no commentary."""


def propose_diff(request: str, output: str) -> list[Op]:
    """Ask the LLM to propose memory updates. Returns possibly-empty list."""
    if not config.MEMORY_PROPOSE_ENABLED:
        return []
    current = read() or "(empty)"
    user_msg = (
        f"User request:\n{request}\n\n"
        f"Agent final output:\n{output[:4000]}\n\n"
        f"Current user.md:\n{current[:4000]}"
    )
    # Use the cheap model when configured.
    msg = _chat_with_model(
        model=config.memory_model(),
        messages=[
            {"role": "system", "content": PROPOSE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        json_mode=True,
    )
    try:
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception:
        return []
    ops_raw = data.get("ops") or []
    out: list[Op] = []
    for op in ops_raw[:20]:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).strip()
        section = str(op.get("section", "")).strip()
        text = str(op.get("text") or "").strip()
        if kind in ("append", "replace", "create_section", "delete") and section:
            out.append({"op": kind, "section": section, "text": text})
    return out


def _chat_with_model(*, model: str, messages, temperature, json_mode) -> dict:
    """Single-shot chat with explicit model override.

    We can't just call llm.chat() because it always uses config.MODEL. We
    duplicate the four lines rather than pull HTTP into this module.
    """
    import requests
    url = f"{config.API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(url, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def render_diff(ops: list[Op]) -> str:
    """Pretty-print ops for the user to review."""
    if not ops:
        return "(no proposed updates)"
    lines: list[str] = []
    for op in ops:
        head = f"[{op['op']}] ## {op['section']}"
        lines.append(head)
        if op["op"] != "delete":
            for ln in op.get("text", "").splitlines():
                lines.append(f"  {ln}")
        lines.append("")
    return "\n".join(lines).rstrip()
