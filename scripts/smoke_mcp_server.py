"""
scripts/smoke_mcp_server.py — manual smoke test for the Janus MCP server
(v1.41.0).

Spawns `python -m janus.mcp.server`, drives the three core MCP methods
(initialize, tools/list, tools/call), and prints what came back.
Exit 0 on success, non-zero on any failure.

This is a manual / CI smoke harness — pytest coverage lives in
tests/test_mcp_server.py.

Run with:
    python scripts/smoke_mcp_server.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time


def _send(proc: subprocess.Popen, envelope: dict) -> None:
    line = json.dumps(envelope) + "\n"
    assert proc.stdin is not None
    proc.stdin.write(line)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen, timeout: float = 30.0) -> dict:
    assert proc.stdout is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line:
            return json.loads(line.strip())
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited early with code {proc.returncode}"
            )
    raise TimeoutError("no response from server within timeout")


def run() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "janus.mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        # 1. initialize
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "smoke", "version": "0"},
                "capabilities": {},
            },
        })
        resp = _recv(proc)
        print("initialize ->", json.dumps(resp, indent=2))
        assert resp.get("id") == 1
        info = (resp.get("result") or {}).get("serverInfo") or {}
        assert info.get("name") == "janus", f"unexpected serverInfo: {info}"

        # 2. tools/list
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        tools = (resp.get("result") or {}).get("tools") or []
        print(f"\ntools/list -> {len(tools)} tools")
        for t in tools:
            print(f"  - {t['name']}")
        names = {t["name"] for t in tools}
        for required in (
            "janus_agent_list",
            "janus_agent_dispatch",
            "janus_blackboard_set",
            "janus_a2a_card",
        ):
            assert required in names, f"missing tool {required}"

        # 3. tools/call janus_agent_list
        _send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "janus_agent_list", "arguments": {}},
        })
        resp = _recv(proc)
        result = resp.get("result") or {}
        content = result.get("content") or []
        assert content and isinstance(content, list)
        text = content[0].get("text", "")
        print("\ntools/call janus_agent_list ->")
        print(text)
        assert "claude" in text, "bundled 'claude' agent should be discoverable"

        # 4. tools/call janus_blackboard_set/get round-trip
        run_id = "mcp-smoke"
        _send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "janus_blackboard_set",
                "arguments": {"run_id": run_id, "key": "ping", "value": "pong"},
            },
        })
        print("\nblackboard set ->", _recv(proc).get("result", {}).get("content"))
        _send(proc, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {
                "name": "janus_blackboard_get",
                "arguments": {"run_id": run_id, "key": "ping"},
            },
        })
        resp = _recv(proc)
        bb_text = (resp.get("result") or {}).get("content", [{}])[0].get("text")
        print("blackboard get ->", bb_text)
        assert bb_text == "pong", f"blackboard round-trip failed: {bb_text!r}"

        # 5. tools/call janus_a2a_card
        _send(proc, {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "janus_a2a_card", "arguments": {}},
        })
        resp = _recv(proc)
        card_text = (resp.get("result") or {}).get("content", [{}])[0].get("text")
        card = json.loads(card_text)
        print(f"\na2a card name={card.get('name')!r}  "
              f"skills={len(card.get('skills') or [])}")
        assert card.get("name") == "Janus"

        # 6. tools/call janus_agent_memory_set/get round-trip
        _send(proc, {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {
                "name": "janus_agent_memory_set",
                "arguments": {
                    "agent": "claude",
                    "key": "last_smoke",
                    "value": "2026-05-11",
                },
            },
        })
        print("\nmemory set ->", _recv(proc).get("result", {}).get("content"))
        _send(proc, {
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {
                "name": "janus_agent_memory_get",
                "arguments": {"agent": "claude", "key": "last_smoke"},
            },
        })
        resp = _recv(proc)
        mem_text = (resp.get("result") or {}).get("content", [{}])[0].get("text")
        print("memory get ->", mem_text)
        assert mem_text == "2026-05-11"

        print("\nALL SMOKE CHECKS PASSED")
        return 0

    finally:
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # Drain stderr so we see the server's logs if something failed.
        if proc.stderr is not None:
            try:
                err = proc.stderr.read() or ""
                if err.strip():
                    print("\n--- server stderr ---")
                    print(err)
            except Exception:
                pass


if __name__ == "__main__":
    try:
        sys.exit(run())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(2)
