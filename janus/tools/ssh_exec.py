"""
tools/ssh_exec.py — model-callable remote command execution (v1.11.0).

WHY THIS EXISTS:
Sam's setup has a bunch of servers (the deploy targets, the Telegram
gateway VPS, etc.). Pre-v1.11 the only way to run something on them
was: model writes a shell script locally → user copies it → user
ssh's in → user pastes it. ssh_exec collapses that to one model tool
call.

WHY SYSTEM SSH (subprocess) NOT PARAMIKO:
1. P6 (no fat SDK in the call path): paramiko adds ~3MB and a crypto
   stack we'd have to keep updated. The system ssh binary already
   exists everywhere this code runs.
2. Reuses ~/.ssh/config: aliases, ProxyJump, ControlMaster, ForwardAgent
   are all defined there already. paramiko would need re-implementation.
3. Reuses ~/.ssh/known_hosts: host-key verification works the way the
   user expects.
4. ssh-agent / hardware keys: subprocess inherits the user's session
   agent for free. paramiko needs explicit agent client setup.
5. Auditability: `ps` shows the actual ssh command. paramiko's calls
   are invisible to the system.

HOW IT WORKS:
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
      -o StrictHostKeyChecking=accept-new \
      <host> -- <command>

  - BatchMode=yes — never prompts for password / passphrase. If keyless
    auth fails, the call fails fast instead of hanging the executor
    waiting for a password the model can't type.
  - ConnectTimeout=10 — fail fast on unreachable hosts.
  - StrictHostKeyChecking=accept-new — first connection adds the host
    to known_hosts; subsequent connections are verified normally. This
    is the modern OpenSSH default and matches what `ssh` does
    interactively for new hosts.
  - -- separator prevents the remote command from being interpreted
    as ssh options (e.g., `-X` would otherwise enable X11 forwarding).

CAPABILITY TOKENS:
  ssh.exec: ["server1.example.com/*", "deploy@vps2/git pull *"]

  Each capability glob matches "<host>/<command>" where <host> is the
  exact host as passed to ssh_exec. Use `host/*` to allow any command
  on that host (model trusts itself), or `host/cmd-prefix *` to scope.

AUTO-MODE PATTERNS:
  Remote commands run through the same auto_mode.analyze_call patterns
  as local shell — `rm -rf /` on a remote host is just as bad. The
  approver layer doesn't distinguish local vs remote. Auto-mode added
  ssh-specific patterns (cf janus.auto_mode) for: ssh keyfile copy,
  authorized_keys mutation, ssh-agent forwarding to untrusted hosts.

GUARDRAILS:
  Registry-level guardrails ALSO apply to ssh_exec output (warns on
  remote git push --force, terraform destroy, etc.).

OUTPUT FORMAT:
  Returns a string the model reads. On success: "exit 0\n<stdout>"
  trimmed to MAX_OUTPUT_BYTES. On non-zero exit: "exit <N>\nSTDOUT:
  ...\nSTDERR: ..." so the model can debug. On connect failure: "error:
  ..." with the underlying ssh error.

P5 (plain-text state): no opaque connection objects. Each call is a
fresh subprocess.

P7 (bounded everything):
  - timeout default 60s, max 600s (matches SHELL_TIMEOUT_MAX)
  - output capped at SSH_OUTPUT_BYTES (default 64 KB)
  - host string capped at 256 chars
  - command string capped at 4 KB
"""

from __future__ import annotations
import os
import re
import shlex
import subprocess
from typing import Callable

from .. import config
from .base import Tool


# Limits independent of shell.py because remote calls have different cost
# profiles (network latency = bigger natural timeouts; remote stdout can
# be big — `journalctl -u nginx` etc.).
SSH_OUTPUT_BYTES = int(os.getenv("JANUS_SSH_OUTPUT_BYTES", str(64 * 1024)))
SSH_TIMEOUT_DEFAULT = int(os.getenv("JANUS_SSH_TIMEOUT", "60"))
SSH_TIMEOUT_MAX = int(os.getenv("JANUS_SSH_TIMEOUT_MAX", "600"))
SSH_HOST_MAX = 256
SSH_COMMAND_MAX = 4096


# Acceptable host shape: alpha[.alpha]* / user@alpha[.alpha]* / IP / IPv6.
# We accept ssh_config aliases too (any non-whitespace token <= 256 chars).
# But we REJECT shell metacharacters in the host so the model can't
# inject options via cleverly-named "hosts".
_HOST_RX = re.compile(r"^[A-Za-z0-9._\-@:\[\]]{1,256}$")


class SshExec(Tool):
    """Run a single command on a remote host via system ssh."""

    name = "ssh_exec"
    description = (
        "Run a command on a REMOTE host via system ssh (BatchMode=yes — "
        "key auth only, no password prompts). Reuses your ~/.ssh/config "
        "aliases, ProxyJump, agent forwarding. Use for: ops on servers, "
        "remote log inspection, deploy scripts, file transfers via "
        "tar/rsync (pipe through ssh). Returns stdout + stderr + exit "
        "code as a string. Capability tokens: ssh.exec: ['<host>/*'] or "
        "ssh.exec: ['<host>/git pull *']."
    )
    parameters = {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": (
                    "Hostname, alias from ~/.ssh/config, or user@host. "
                    "Examples: 'production-1', 'deploy@vps.example.com', "
                    "'192.168.1.10'. Must NOT contain shell metacharacters."
                ),
            },
            "command": {
                "type": "string",
                "description": (
                    "Command to run remotely. Quoted as ONE shell string "
                    "by ssh — for multi-line scripts use ';' or '&&' "
                    "separators. Avoid stdin-reading commands (BatchMode "
                    "won't supply input)."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Seconds before the call is killed. Default "
                    f"{SSH_TIMEOUT_DEFAULT}, hard-capped at "
                    f"{SSH_TIMEOUT_MAX} (P7 — model can't request "
                    f"a 6-hour ssh hold)."
                ),
            },
            "working_dir": {
                "type": "string",
                "description": (
                    "Optional remote directory to cd into BEFORE running "
                    "the command. e.g., '/opt/app'. Quoted for shell "
                    "safely; pass an absolute path."
                ),
            },
        },
        "required": ["host", "command"],
    }
    risk = "exec"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        host = (args.get("host") or "").strip()
        command = (args.get("command") or "")  # don't strip — preserve newlines
        if not host:
            return "error: host required"
        if not command.strip():
            return "error: command required"
        if len(host) > SSH_HOST_MAX:
            return f"error: host too long (>{SSH_HOST_MAX} chars)"
        if not _HOST_RX.match(host):
            return (
                f"error: host {host!r} contains disallowed characters. "
                f"Use the hostname only (alias / IP / user@host)."
            )
        if len(command) > SSH_COMMAND_MAX:
            return f"error: command too long (>{SSH_COMMAND_MAX} chars)"

        # Timeout clamp — same lesson as shell.py SHELL_TIMEOUT_MAX
        # (model passed timeout=600000 in v1.1.1 thinking it was ms).
        try:
            timeout = int(args.get("timeout") or SSH_TIMEOUT_DEFAULT)
        except (TypeError, ValueError):
            return "error: timeout must be an integer (seconds)"
        timeout = max(1, min(timeout, SSH_TIMEOUT_MAX))

        working_dir = (args.get("working_dir") or "").strip()
        if working_dir:
            # Build "cd <wd> && <cmd>" — wd quoted, command passed
            # through verbatim (the user-shell on the remote handles
            # the rest of the parsing).
            full_command = f"cd {shlex.quote(working_dir)} && ({command})"
        else:
            full_command = command

        # Capability key is "<host>/<command>" so capability globs can
        # scope to host and to a command prefix.
        cap_key = f"{host}/{command[:80]}"
        if not approver(
            f"ssh_exec → {host}",
            f"$ {command[:300]}{'...' if len(command) > 300 else ''}",
            capability=("ssh", "exec", cap_key),
        ):
            return f"refused: ssh_exec({host})"

        ssh_args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={min(timeout, 10)}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            host,
            "--",
            full_command,
        ]

        try:
            proc = subprocess.run(
                ssh_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (
                f"error: ssh timeout after {timeout}s on {host}. The "
                f"remote command may still be running — check there."
            )
        except FileNotFoundError:
            return (
                "error: ssh binary not found on this system. Install "
                "OpenSSH client (apt install openssh-client / brew "
                "install openssh / Add 'OpenSSH.Client' Windows feature)."
            )
        except OSError as e:
            return f"error: ssh failed to launch: {type(e).__name__}: {e}"

        stdout = (proc.stdout or "")[:SSH_OUTPUT_BYTES]
        stderr = (proc.stderr or "")[:SSH_OUTPUT_BYTES]
        truncated_stdout = len(proc.stdout or "") > SSH_OUTPUT_BYTES
        truncated_stderr = len(proc.stderr or "") > SSH_OUTPUT_BYTES

        if proc.returncode == 0:
            tail = "\n[stdout truncated]" if truncated_stdout else ""
            return f"exit 0\n{stdout}{tail}"

        # Non-zero — surface stderr too.
        parts = [f"exit {proc.returncode}"]
        if stdout:
            parts.append("STDOUT:")
            parts.append(stdout + ("\n[truncated]" if truncated_stdout else ""))
        if stderr:
            parts.append("STDERR:")
            parts.append(stderr + ("\n[truncated]" if truncated_stderr else ""))
        return "\n".join(parts)
