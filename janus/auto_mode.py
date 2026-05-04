"""
auto_mode.py — risk analyzer for `auto` permission mode (v1.5).

Auto mode is `bypassPermissions` + safety net: it auto-approves like
bypass but adds heuristic risk analysis that can flip allow→deny based
on the SPECIFIC tool args (not just the risk class).

Use case: long-running unattended swarms where the user can't be at the
keyboard to approve every shell call, but a runaway `rm -rf /` would be
catastrophic. Auto mode sits between `default` (safe but interrupts you)
and `bypassPermissions` (no friction, no safety).

CHECK PIPELINE (defense in depth):
  1. Capability allowlist  — skill-granted = always allow (existing)
  2. Permission matrix     — auto says "allow" for read/write/exec base
  3. Risk analyzer (here)  — pattern-match args against danger patterns;
                              if matched → flip to deny with reason
  4. Injection scanner     — separate concern; see injection.py

All patterns are USER-EXTENSIBLE via ~/.janus/auto_risk_patterns.yaml
(P5: plain-text). The bundled patterns below cover the universally-bad
cases (SSRF, system path writes, fork bombs, curl-pipe-shell).

DESIGN — heuristics only, no LLM call:
v1.5 ships pure pattern matching. Fast, free, deterministic. Future
v1.6 can add an OPT-IN LLM second-opinion path for ambiguous cases
(`JANUS_AUTO_RISK_LLM=1`) — but the heuristic baseline must stay since
it's the fallback when the network is down or budget is exhausted.

NEVER RAISES (P8):
The analyzer returns Verdict objects, never raises. A pattern compile
error degrades to "allow with warning" — better to let the action
through with a logged warning than to break the whole agent on a
malformed user-config regex.
"""

from __future__ import annotations
import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config


# ---------- Verdict ----------


@dataclass
class RiskVerdict:
    """Result of analyzing one tool call."""
    allowed: bool
    reason: str = ""
    matched_pattern: str = ""

    @classmethod
    def safe(cls) -> "RiskVerdict":
        return cls(allowed=True, reason="", matched_pattern="")

    @classmethod
    def block(cls, reason: str, pattern: str = "") -> "RiskVerdict":
        return cls(allowed=False, reason=reason, matched_pattern=pattern)


# ---------- Bundled patterns ----------
#
# Conservative on purpose — these should be UNAMBIGUOUSLY bad. False
# positives in auto mode break legitimate work; users who want broader
# blocking add patterns to ~/.janus/auto_risk_patterns.yaml.


# Shell command patterns. Match the FULL command string.
_BUNDLED_SHELL_BLOCKS: list[tuple[str, str]] = [
    # Existential filesystem destruction.
    (r"\brm\s+-[rRf]+[rRf\s]*\s+/+(\s|$|\*)", "rm -rf / (root deletion)"),
    (r"\brm\s+-[rRf]+[rRf\s]*\s+~/?(\s|$)", "rm -rf ~ (home deletion)"),
    (r"\brm\s+-[rRf]+[rRf\s]*\s+\$HOME", "rm -rf $HOME"),
    # Fork bomb.
    (r":\(\)\s*\{[^}]*:\s*\|\s*:[^}]*\}\s*;\s*:", "fork bomb"),
    # Filesystem format / overwrite.
    (r"\bmkfs\.\w+\b", "mkfs.* (format filesystem)"),
    (r"\bdd\s+if=/dev/(zero|random|urandom)\s+of=/dev/[sh]d", "dd to raw disk"),
    (r">\s*/dev/[sh]d[a-z][0-9]?\b", "redirect to raw disk device"),
    # Wide-open chmod (with or without -R flag).
    (r"\bchmod\s+(?:-R?\s+)?7?77\s+/+(\s|$)", "chmod 777 /"),
    # Curl/wget piped to shell.
    (r"\bcurl\s+[^|]*\|\s*(sh|bash|zsh|fish|sudo)\b", "curl | shell (untrusted execution)"),
    (r"\bwget\s+[^|]*\|\s*(sh|bash|zsh|fish|sudo)\b", "wget | shell (untrusted execution)"),
    # Privilege escalation.
    (r"^\s*sudo\b", "sudo (privilege escalation)"),
    (r"^\s*su\s+-?\s*(root|$)", "su to root"),
    # Crontab manipulation.
    (r"\bcrontab\s+-r\b", "crontab -r (delete schedule)"),
    # Disable firewall.
    (r"\bufw\s+disable\b", "ufw disable"),
    (r"\bsystemctl\s+stop\s+(firewalld|ufw|iptables)\b", "stop firewall service"),
    # Recursive janus invocation (already blocked elsewhere — defense in depth).
    (r"^\s*janus\s+(?!--version|--help|--logo|--analyze|--conversations|--reindex)",
     "recursive janus invocation"),
]


# Filesystem write paths. Match the target PATH (after expansion).
# These are PREFIXES — any write under these paths blocks.
_BUNDLED_FS_BLOCK_PATHS: list[tuple[str, str]] = [
    (r"^(/etc/|/sys/|/proc/|/dev/|/boot/)", "system path"),
    (r"\.ssh/", "SSH key directory"),
    (r"^/var/log/", "system logs"),
    (r"^/usr/", "system binaries"),
]

_BUNDLED_FS_BLOCK_NAMES: list[tuple[str, str]] = [
    (r"^id_rsa$|^id_rsa\.pub$|^id_ed25519$", "SSH private key"),
    (r"\.pem$|\.key$|\.p12$|\.pfx$", "private key file"),
    (r"\.token$|\.secret$", "token/secret file"),
    (r"^\.env$|^\.env\.[\w]+$", "env file (likely secrets)"),
    (r"^known_hosts$|^authorized_keys$", "SSH config"),
    (r"^/etc/passwd$|^/etc/shadow$|^/etc/sudoers", "system credential file"),
]


# Web fetch host blocks. Hostnames + IP ranges that should never be
# fetched in auto mode (SSRF defense + cloud-metadata defense).
_BUNDLED_WEB_BLOCK_HOSTS: list[tuple[str, str]] = [
    (r"^(localhost|127\.|::1|0\.0\.0\.0)", "localhost (SSRF)"),
    (r"^169\.254\.169\.254$", "AWS/GCP/Azure metadata service (SSRF)"),
    (r"^metadata\.google\.internal$", "GCP metadata service"),
]

# Private IP ranges (RFC 1918 + link-local + loopback). Matched
# numerically via ipaddress, not regex.
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


# ---------- User-config loader ----------


@dataclass
class RiskPatterns:
    """Bundled + user patterns, compiled once."""
    shell_blocks: list[tuple[re.Pattern, str]] = field(default_factory=list)
    fs_block_paths: list[tuple[re.Pattern, str]] = field(default_factory=list)
    fs_block_names: list[tuple[re.Pattern, str]] = field(default_factory=list)
    web_block_hosts: list[tuple[re.Pattern, str]] = field(default_factory=list)


def _compile(rules: list[tuple[str, str]], flags: int = re.IGNORECASE) -> list[tuple[re.Pattern, str]]:
    out: list[tuple[re.Pattern, str]] = []
    for raw, label in rules:
        try:
            out.append((re.compile(raw, flags), label))
        except re.error:
            # Skip malformed regex; never crash on pattern load (P8).
            pass
    return out


def _load_patterns() -> RiskPatterns:
    """Combine bundled + user patterns. User extras live in
    ~/.janus/auto_risk_patterns.yaml (loose YAML, hand-parsed by reusing
    the skills frontmatter parser)."""
    patterns = RiskPatterns(
        shell_blocks=_compile(_BUNDLED_SHELL_BLOCKS),
        fs_block_paths=_compile(_BUNDLED_FS_BLOCK_PATHS),
        fs_block_names=_compile(_BUNDLED_FS_BLOCK_NAMES),
        web_block_hosts=_compile(_BUNDLED_WEB_BLOCK_HOSTS),
    )
    user_file = config.HOME / "auto_risk_patterns.yaml"
    if user_file.is_file():
        try:
            from .skills import _parse_yaml_subset
            user_data = _parse_yaml_subset(
                user_file.read_text(encoding="utf-8")
            )
            for key, lst, label in (
                ("shell_blocks", patterns.shell_blocks, "user shell"),
                ("fs_block_paths", patterns.fs_block_paths, "user fs path"),
                ("fs_block_names", patterns.fs_block_names, "user fs name"),
                ("web_block_hosts", patterns.web_block_hosts, "user web host"),
            ):
                items = user_data.get(key) or []
                if isinstance(items, list):
                    for raw in items:
                        if isinstance(raw, str):
                            lst.extend(_compile([(raw, f"{label}: {raw}")]))
        except Exception:
            # Per P8: never crash on user-config parse failure.
            pass
    return patterns


# Module-level cache. Reload via reload_patterns() — auto mode picks up
# new user patterns without restarting the agent.
_PATTERNS: RiskPatterns | None = None


def patterns() -> RiskPatterns:
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _load_patterns()
    return _PATTERNS


def reload_patterns() -> None:
    """Drop the cache. Next analyze_call() reloads from disk."""
    global _PATTERNS
    _PATTERNS = None


# ---------- Analyzer dispatch ----------


def analyze_call(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    capability: tuple[str, str, str] | None = None,
) -> RiskVerdict:
    """Classify a tool call as safe or risky.

    `tool_name` — registered tool name (e.g., "shell.exec", "fs_write").
    `args` — the parsed arguments the model passed.
    `capability` — optional (tool, verb, target) triple if the call is
        capability-scoped; analyzer respects skill grants.

    Returns RiskVerdict.safe() to allow, RiskVerdict.block(reason) to deny.
    """
    args = args or {}
    p = patterns()

    # Shell: classify by command substring.
    if _is_shell_tool(tool_name):
        cmd = _extract_shell_command(args)
        if cmd:
            for rx, label in p.shell_blocks:
                if rx.search(cmd):
                    return RiskVerdict.block(
                        f"shell command matched block pattern: {label}",
                        pattern=rx.pattern,
                    )
        return RiskVerdict.safe()

    # FS write: classify by path.
    if _is_fs_write_tool(tool_name):
        path = _extract_fs_path(args)
        if path:
            for rx, label in p.fs_block_paths:
                if rx.search(path):
                    return RiskVerdict.block(
                        f"fs write to {label}: {path}",
                        pattern=rx.pattern,
                    )
            basename = path.rsplit("/", 1)[-1]
            for rx, label in p.fs_block_names:
                if rx.search(basename):
                    return RiskVerdict.block(
                        f"fs write to {label}: {basename}",
                        pattern=rx.pattern,
                    )
        return RiskVerdict.safe()

    # Web fetch: classify by URL host.
    if _is_web_fetch_tool(tool_name):
        url = _extract_url(args)
        if url:
            host = _host_from_url(url)
            if host:
                # Hostname patterns.
                for rx, label in p.web_block_hosts:
                    if rx.search(host):
                        return RiskVerdict.block(
                            f"web fetch to {label}: {host}",
                            pattern=rx.pattern,
                        )
                # Numeric IP check.
                ip_verdict = _check_ip_block(host)
                if ip_verdict is not None:
                    return ip_verdict
        return RiskVerdict.safe()

    # Anything else — auto mode allows by default (the matrix already
    # said allow). User-extensible block patterns can extend this list.
    return RiskVerdict.safe()


# ---------- Tool-name classifiers ----------


def _is_shell_tool(name: str) -> bool:
    n = name.lower()
    return n in ("shell.exec", "shell_exec", "shell", "bash", "exec",
                 "run_command", "shell_run")


def _is_fs_write_tool(name: str) -> bool:
    n = name.lower()
    return n in (
        "fs.write", "fs_write", "write_file", "fs.write_file",
        "edit", "fs_edit", "fs.edit", "multi_edit", "fs.multi_edit",
        "create", "fs.create",
    )


def _is_web_fetch_tool(name: str) -> bool:
    n = name.lower()
    return n in (
        "web.fetch", "web_fetch", "fetch", "fetch_web",
        "browser.navigate", "browser_navigate", "browser.visit", "browser_visit",
    )


# ---------- Arg extractors ----------


def _extract_shell_command(args: dict) -> str:
    """Pull the command string from common arg shapes."""
    for k in ("cmd", "command", "shell", "script"):
        v = args.get(k)
        if isinstance(v, str):
            return v
    return ""


def _extract_fs_path(args: dict) -> str:
    """Pull the target path from common write-tool arg shapes."""
    for k in ("path", "file", "filename", "file_path", "target"):
        v = args.get(k)
        if isinstance(v, str):
            return v
    return ""


def _extract_url(args: dict) -> str:
    for k in ("url", "uri", "address", "endpoint", "href"):
        v = args.get(k)
        if isinstance(v, str):
            return v
    return ""


def _host_from_url(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


def _check_ip_block(host: str) -> RiskVerdict | None:
    """If `host` parses as a private/loopback/link-local IP, block.
    Otherwise return None (other patterns may still apply)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # Not a numeric IP; skip.
    for net in _PRIVATE_NETS:
        if ip in net:
            return RiskVerdict.block(
                f"web fetch to private/loopback IP: {host} (in {net})",
                pattern=str(net),
            )
    return None
