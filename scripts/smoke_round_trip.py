"""
scripts/smoke_round_trip.py — full bidirectional round-trip test for v1.41.0.

Simulates what Claude Code will do once its MCP client picks up the
janus server: spawn `python -m janus.mcp.server`, call
`janus_agent_dispatch` with name=claude, prompt="..." — which routes
through janus.agents.dispatch → bundled claude agent (wrapper) →
ClaudeCode tool → `claude -p` subprocess → Anthropic API → response
all the way back.

Prereqs:
  * `claude` binary on PATH (run `where claude` to check)
  * `claude login` completed at least once

Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time


def _send(proc, envelope):
    line = json.dumps(envelope) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()


def _recv(proc, timeout=180.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line:
            return json.loads(line.strip())
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early (rc={proc.returncode})")
    raise TimeoutError(f"no response within {timeout}s")


def main():
    proc = subprocess.Popen(
        [sys.executable, "-m", "janus.mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "clientInfo": {"name": "round-trip", "version": "0"},
                       "capabilities": {}},
        })
        init = _recv(proc)
        print(f"server: {init['result']['serverInfo']}")

        sentinel = "ROUND_TRIP_OK_42"
        prompt = f"Reply with exactly: {sentinel}. No other words."

        print("dispatching to 'claude' agent via MCP...")
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "janus_agent_dispatch",
                "arguments": {"name": "claude", "prompt": prompt},
            },
        })
        # Allow up to 180s for the spawned `claude -p` subprocess.
        resp = _recv(proc, timeout=180.0)
        result = resp.get("result") or {}
        is_error = result.get("isError")
        text = (result.get("content") or [{}])[0].get("text", "")
        print(f"isError: {is_error}")
        print(f"response: {text!r}")

        if is_error:
            print(f"FAIL: tool returned isError. Body: {text}")
            return 1
        if sentinel not in text:
            print(f"FAIL: sentinel {sentinel!r} not in response")
            return 1

        print("PASS: full Claude-Code <-> MCP <-> Janus <-> claude round trip works")
        return 0

    finally:
        if proc.poll() is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if proc.stderr is not None:
            try:
                err = proc.stderr.read() or ""
                if err.strip():
                    print("--- server stderr ---")
                    print(err)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
