"""
tools/capabilities.py — capability tokens.

Token grammar:
  tool_name "." verb ":" "[" globs "]"

Examples:
  shell.exec:  ["git *", "pnpm *"]
  fs.write:    ["src/**", "tests/**"]
  fs.read:     ["**"]
  web.fetch:   ["https://docs.python.org/*"]

Matching:
  fnmatch.fnmatchcase with one extension — `**` matches across `/`. We
  walk a small implementation rather than pull pathspec/globmatch in.

Capability checks are an *additional* layer on top of the existing approver
flow. `dangerous=True` tools still call the approver — but the approver will
short-circuit to True when the active skill's tokens grant the action.

WHY THIS DESIGN:
  Defense is structural. The user reads a skill's capabilities ONCE at
  promotion time and then trusts that the agent can't exceed them. There's
  no "this innocuous-looking shell command actually does X" surprise — if
  it doesn't match the glob, it gets the y/n prompt.
"""

from __future__ import annotations
import fnmatch
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    """A single capability grant: tool.verb -> [globs].

    Convenience: Capability("shell", "exec", ("git *", "pnpm *"))
    """
    tool: str
    verb: str
    globs: tuple[str, ...]

    def matches(self, tool: str, verb: str, target: str) -> bool:
        if tool != self.tool or verb != self.verb:
            return False
        return any(_glob_match(g, target) for g in self.globs)


@dataclass
class CapabilitySet:
    """A bundle of capabilities. Empty = grants nothing extra."""
    caps: list[Capability] = field(default_factory=list)

    def grants(self, tool: str, verb: str, target: str) -> bool:
        return any(c.matches(tool, verb, target) for c in self.caps)

    @classmethod
    def from_dict(cls, d: dict | None) -> "CapabilitySet":
        """Parse YAML-frontmatter-style dict.

        Input shape:
            {
              "shell.exec": ["git *", "pnpm *"],
              "fs.write":   ["src/**"]
            }
        """
        if not d:
            return cls()
        out: list[Capability] = []
        for key, globs in d.items():
            if "." not in key:
                continue
            tool, verb = key.split(".", 1)
            if not isinstance(globs, list):
                continue
            out.append(Capability(
                tool=tool.strip(),
                verb=verb.strip(),
                globs=tuple(str(g) for g in globs if g),
            ))
        return cls(caps=out)

    def render(self) -> str:
        """Pretty-print for review by the user."""
        if not self.caps:
            return "(no capabilities)"
        lines = []
        for c in self.caps:
            globs = ", ".join(repr(g) for g in c.globs)
            lines.append(f"  {c.tool}.{c.verb}: [{globs}]")
        return "\n".join(lines)


# ---------- Glob matcher with `**` support ----------


def _glob_match(pattern: str, target: str) -> bool:
    """Like fnmatch but `**` crosses `/` boundaries.

    We compile to a regex once. Cheap for our scale (handful of globs per
    skill, a handful of skills).
    """
    rx = _compile(pattern)
    return rx.match(target) is not None


_COMPILE_CACHE: dict[str, re.Pattern] = {}


def _compile(pattern: str) -> re.Pattern:
    if pattern in _COMPILE_CACHE:
        return _COMPILE_CACHE[pattern]
    # Token-aware translation: `**` first, then `*` and `?` separately.
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            out.append(".*")
            i += 2
            # consume an optional following slash so `src/**/x` matches `src/x`
            if i < len(pattern) and pattern[i] in ("/", "\\"):
                i += 1
        elif c == "*":
            out.append(r"[^/\\]*")
            i += 1
        elif c == "?":
            out.append(r"[^/\\]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    rx = re.compile("^" + "".join(out) + "$")
    _COMPILE_CACHE[pattern] = rx
    return rx
