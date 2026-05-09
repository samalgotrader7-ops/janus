"""Tests for v1.31.14 — MCP load-skip diagnostics + web version log.

FIELD-VALIDATION FINDING (Sam, 2026-05-09):

Sam configured 13 MCP servers in ~/.janus/mcp/servers.json (via the
Telegram bot writing to disk via fs_write). Reported "CLI and Web
don't show any MCP servers". Two distinct issues surfaced:

1. ``tinyfish`` MCP entry had ``"url": "..."`` (HTTP transport) but
   no ``"command"`` field. ``load_servers()`` silently skipped it
   with ``if not isinstance(spec, dict) or "command" not in spec:
   continue``. No log, no warning — invisible drop.

2. The running ``janus web`` process was 2 days old (started May 7)
   while /opt/janus disk had v1.31.13. All v1.29-v1.31 endpoints
   returned 404 because the running process held old imported code
   in memory. Without a version on the startup banner, "is this
   process stale?" required digging through pipx, ps, and curl.

THE FIX:

Part A — load_servers diagnostics:
  - New SkipReason dataclass: (name, source, reason)
  - New load_servers_with_diagnostics() returning (servers, skipped)
  - load_servers() preserved as back-compat shim returning just dict
  - cli_rich /mcp catalog renders skipped entries (yellow ⚠ rows)
  - web /api/mcp/catalog response includes ``skipped`` array
  - Frontend MCP panel renders skipped entries with reason

Part B — web version log:
  - serve() startup print now includes branding.VERSION
  - "janus web UI v1.31.14 on http://..." instead of just URL
  - Future stale-process bugs visible at a glance via head of log

DESIGN INVARIANTS PINNED:
  * load_servers() back-compat — returns just dict[str, McpServerConfig]
  * load_servers_with_diagnostics() returns (dict, list[SkipReason])
  * SkipReason has name, source, reason fields
  * HTTP transport (url-only) entries get a clear reason mentioning
    stdio-only support
  * Missing-command entries get a different, distinguishable reason
  * Malformed (non-dict) entries get yet another reason
  * Duplicate names get a "duplicate" reason
  * Web JSON response shape: {servers: [...], skipped: [{name, reason, source}]}
  * cli_rich source-pin: load_servers_with_diagnostics is called
  * Web serve() print includes "v" + branding.VERSION
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from janus.mcp import client as mcp_client
from janus.mcp.client import (
    McpServerConfig,
    SkipReason,
    SKIP_REASON_DUPLICATE,
    SKIP_REASON_HTTP_PREFIX,
    SKIP_REASON_MISSING_COMMAND,
    SKIP_REASON_NOT_DICT,
    load_servers,
    load_servers_with_diagnostics,
)


# -------------------- Part A: load_servers diagnostics --------------------


@pytest.fixture
def isolated_mcp_config(tmp_path, monkeypatch):
    """Point the loader at a tmp config file. Returns a writer
    callable that takes a dict and writes it as the servers config."""
    cfg_dir = tmp_path / ".janus" / "mcp"
    cfg_dir.mkdir(parents=True)
    cfg_path = cfg_dir / "servers.json"
    claude_path = tmp_path / ".claude" / "settings.json"
    claude_path.parent.mkdir(parents=True)
    monkeypatch.setattr(
        "janus.config.MCP_SERVERS_FILE", cfg_path,
    )
    monkeypatch.setattr(
        "janus.config.CLAUDE_SETTINGS_FILE", claude_path,
    )

    def write(payload, target="janus"):
        target_path = cfg_path if target == "janus" else claude_path
        target_path.write_text(json.dumps(payload), encoding="utf-8")

    return write


def test_skip_reason_is_a_dataclass():
    """SkipReason has name, source, reason fields."""
    r = SkipReason(name="x", source="/tmp/y", reason="z")
    assert r.name == "x"
    assert r.source == "/tmp/y"
    assert r.reason == "z"


def test_skip_reason_constants_exist_as_module_strings():
    """Reason templates are module constants so tests can pin
    without coupling to in-function wording."""
    assert isinstance(SKIP_REASON_NOT_DICT, str)
    assert isinstance(SKIP_REASON_HTTP_PREFIX, str)
    assert isinstance(SKIP_REASON_MISSING_COMMAND, str)
    assert isinstance(SKIP_REASON_DUPLICATE, str)
    # HTTP prefix mentions stdio so the user knows what's supported.
    assert "stdio" in SKIP_REASON_HTTP_PREFIX.lower()
    assert "http" in SKIP_REASON_HTTP_PREFIX.lower()


def test_load_servers_back_compat_returns_just_dict(isolated_mcp_config):
    """Existing callers must keep getting dict[str, McpServerConfig]
    (no tuple unpacking expected)."""
    isolated_mcp_config({
        "mcpServers": {
            "ok": {"command": "npx", "args": ["server"]},
        }
    })
    result = load_servers()
    assert isinstance(result, dict)
    assert "ok" in result
    assert isinstance(result["ok"], McpServerConfig)


def test_load_servers_with_diagnostics_returns_tuple(isolated_mcp_config):
    """New API returns (dict, list[SkipReason])."""
    isolated_mcp_config({"mcpServers": {"ok": {"command": "npx"}}})
    result = load_servers_with_diagnostics()
    assert isinstance(result, tuple)
    assert len(result) == 2
    servers, skipped = result
    assert isinstance(servers, dict)
    assert isinstance(skipped, list)


def test_http_transport_entry_skipped_with_clear_reason(isolated_mcp_config):
    """The exact bug from Sam's VPS — tinyfish-style URL-only entry
    is skipped, but the reason explains why."""
    isolated_mcp_config({
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["server"]},
            "tinyfish": {"url": "https://mcp.example.com/sse"},
        }
    })
    servers, skipped = load_servers_with_diagnostics()
    assert "filesystem" in servers
    assert "tinyfish" not in servers
    assert len(skipped) == 1
    sk = skipped[0]
    assert sk.name == "tinyfish"
    assert "https://mcp.example.com/sse" in sk.reason
    assert "stdio" in sk.reason.lower()


def test_missing_command_no_url_gets_distinguishable_reason(isolated_mcp_config):
    """Entry with neither command nor url — different reason than
    HTTP transport so the user can fix the right thing."""
    isolated_mcp_config({
        "mcpServers": {
            "broken": {"args": ["foo"]},
        }
    })
    servers, skipped = load_servers_with_diagnostics()
    assert "broken" not in servers
    assert len(skipped) == 1
    assert skipped[0].name == "broken"
    assert skipped[0].reason == SKIP_REASON_MISSING_COMMAND
    # Distinct from HTTP reason
    assert "stdio" not in skipped[0].reason.lower()


def test_non_dict_entry_skipped_with_not_dict_reason(isolated_mcp_config):
    """Malformed entries (where the value isn't a JSON object) also
    surface a reason — instead of being silently dropped."""
    isolated_mcp_config({
        "mcpServers": {
            "ok": {"command": "npx"},
            "bad": "this should be a dict",
        }
    })
    servers, skipped = load_servers_with_diagnostics()
    assert "ok" in servers
    assert "bad" not in servers
    assert any(s.name == "bad" and s.reason == SKIP_REASON_NOT_DICT
               for s in skipped)


def test_duplicate_across_sources_reported(isolated_mcp_config):
    """When ~/.claude/settings.json defines a server already in
    ~/.janus/mcp/servers.json, the duplicate is skipped — but with
    a clear reason instead of a silent overwrite."""
    isolated_mcp_config(
        {"mcpServers": {"shared": {"command": "npx", "args": ["a"]}}},
        target="janus",
    )
    isolated_mcp_config(
        {"mcpServers": {"shared": {"command": "uvx", "args": ["b"]}}},
        target="claude",
    )
    servers, skipped = load_servers_with_diagnostics()
    # Janus wins
    assert servers["shared"].command == "npx"
    # Claude duplicate surfaced
    assert any(s.name == "shared" and s.reason == SKIP_REASON_DUPLICATE
               for s in skipped)


def test_full_sam_vps_shape_replicated(isolated_mcp_config):
    """End-to-end pin: 13 entries in JSON (12 valid + 1 HTTP-only),
    matches the actual disk contents on Sam's VPS at v1.31.13."""
    isolated_mcp_config({
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["@mcp/fs"]},
            "git": {"command": "uvx", "args": ["mcp-git"]},
            "sqlite": {"command": "npx", "args": ["@mcp/sqlite"]},
            "memory": {"command": "npx", "args": ["@mcp/memory"]},
            "fetch": {"command": "npx", "args": ["@mcp/fetch"]},
            "sequentialthinking": {"command": "npx", "args": ["@mcp/seq"]},
            "time": {"command": "npx", "args": ["@mcp/time"]},
            "yahoofinance": {"command": "npx", "args": ["yf"]},
            "docker": {"command": "npx", "args": ["mcp-docker"]},
            "playwright": {"command": "npx", "args": ["@pw/mcp"]},
            "shodan": {
                "command": "npx",
                "args": ["@b/mcp-shodan"],
                "env": {"SHODAN_API_KEY": "x"},
            },
            "postgres": {
                "command": "npx",
                "args": ["mcp-pg"],
                "disabled": True,
            },
            "tinyfish": {"url": "https://mcp.tinyfish.example/sse"},
        }
    })
    servers, skipped = load_servers_with_diagnostics()
    assert len(servers) == 12
    assert "tinyfish" not in servers
    assert "postgres" in servers
    assert servers["postgres"].enabled is False
    assert any(s.name == "tinyfish" for s in skipped)


def test_no_skipped_means_empty_list_not_none(isolated_mcp_config):
    """skipped is always a list, never None — frontend can iterate
    safely without null-checks."""
    isolated_mcp_config({
        "mcpServers": {"ok": {"command": "npx"}}
    })
    _, skipped = load_servers_with_diagnostics()
    assert skipped == []


def test_parse_failure_logs_warning(isolated_mcp_config, caplog):
    """Unparseable JSON gets logged at WARNING level (no longer
    silent). Pre-v1.31.14 the bare ``except Exception: continue``
    swallowed parse errors entirely."""
    import logging
    cfg_path = Path(mcp_client.config.MCP_SERVERS_FILE)
    cfg_path.write_text("{ broken json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="janus.mcp"):
        servers, skipped = load_servers_with_diagnostics()
    # No servers (parse failed)
    assert servers == {}
    # Warning logged
    assert any("parse failed" in rec.message for rec in caplog.records)


# -------------------- Source pins --------------------


def test_mcp_client_source_pins_diagnostics_function():
    """Source-pin: the diagnostics function exists and is exported."""
    src = Path(mcp_client.__file__).read_text(encoding="utf-8")
    assert "def load_servers_with_diagnostics(" in src
    assert "class SkipReason" in src
    # Module-level logger present (v1.31.14 marker)
    assert 'logging.getLogger("janus.mcp")' in src
    # v1.31.14 comment marker
    assert "v1.31.14" in src


def test_mcp_init_re_exports_new_symbols():
    """Source-pin: __init__ re-exports SkipReason +
    load_servers_with_diagnostics so callers can import from the
    package root."""
    from janus.mcp import (
        SkipReason as ExportedSkipReason,
        load_servers_with_diagnostics as exported_loader,
    )
    assert ExportedSkipReason is SkipReason
    assert exported_loader is load_servers_with_diagnostics


def test_cli_rich_mcp_catalog_uses_diagnostics():
    """Source-pin: /mcp catalog now calls
    load_servers_with_diagnostics so users see skipped entries."""
    cli_rich_path = (
        Path(mcp_client.__file__).parent.parent / "cli_rich.py"
    )
    src = cli_rich_path.read_text(encoding="utf-8")
    # The catalog command unpacks both servers and skipped
    catalog_block = src[src.index("def _cmd_mcp_catalog"):]
    catalog_block = catalog_block[: catalog_block.index("\ndef ", 50)]
    assert "load_servers_with_diagnostics" in catalog_block
    # Renders the skipped list with the warning glyph
    assert "skipped:" in catalog_block
    # v1.31.14 marker present so future maintainers can grep
    assert "v1.31.14" in catalog_block


def test_web_mcp_catalog_returns_skipped_field():
    """Source-pin: /api/mcp/catalog response shape now includes
    'skipped' alongside 'servers'."""
    web_path = (
        Path(mcp_client.__file__).parent.parent / "gateways" / "web.py"
    )
    src = web_path.read_text(encoding="utf-8")
    # The endpoint uses the diagnostics function. Slice from the
    # endpoint header through the next route definition so we don't
    # truncate mid-handler (the response builder is ~80 lines below).
    catalog_idx = src.index('"/api/mcp/catalog"')
    next_route_idx = src.index("@app.", catalog_idx + 1)
    block = src[catalog_idx:next_route_idx]
    assert "load_servers_with_diagnostics" in block
    assert '"skipped"' in block
    assert "skipped_out" in block


# -------------------- Part B: web version log --------------------


def test_web_serve_prints_version_on_startup():
    """Source-pin: serve() startup banner includes the version
    string so future stale-process bugs are visible from a log
    head."""
    web_path = (
        Path(mcp_client.__file__).parent.parent / "gateways" / "web.py"
    )
    src = web_path.read_text(encoding="utf-8")
    serve_idx = src.index("def serve(")
    serve_block = src[serve_idx: serve_idx + 3500]
    # New format: "janus web UI v{branding.VERSION} on http://..."
    assert "branding.VERSION" in serve_block
    assert "janus web UI v" in serve_block
    # v1.31.14 marker
    assert "v1.31.14" in serve_block


def test_telegram_serve_already_prints_version():
    """Telegram already had version-on-startup since v1.31.9 — pin
    that it didn't regress while we were touching neighboring code."""
    tg_path = (
        Path(mcp_client.__file__).parent.parent / "gateways" / "telegram.py"
    )
    src = tg_path.read_text(encoding="utf-8")
    assert "janus telegram gateway running" in src
    assert "branding.VERSION" in src


# -------------------- Web endpoint behavioral test --------------------


def test_web_api_mcp_catalog_includes_skipped_via_testclient(
    isolated_mcp_config, monkeypatch
):
    """Behavioral via FastAPI TestClient — verify the /api/mcp/catalog
    JSON response includes the skipped list with name + reason.

    Skip if FastAPI not installed (matches the optional-dep gate)."""
    pytest.importorskip("fastapi")

    isolated_mcp_config({
        "mcpServers": {
            "good": {"command": "npx", "args": ["x"]},
            "tinyfish": {"url": "https://example/sse"},
        }
    })
    # Localhost auth bypass keeps the test focused on the data shape.
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    from janus.gateways import web as web_module
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/api/mcp/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert "servers" in data
    assert "skipped" in data
    server_names = {s["name"] for s in data["servers"]}
    assert "good" in server_names
    assert "tinyfish" not in server_names
    skipped_names = {s["name"] for s in data["skipped"]}
    assert "tinyfish" in skipped_names
    tf_entry = next(s for s in data["skipped"] if s["name"] == "tinyfish")
    assert "reason" in tf_entry
    assert "source" in tf_entry
    assert "https://example/sse" in tf_entry["reason"]


# -------------------- Web frontend (app.js) source pins --------------------


def test_app_js_renders_skipped_entries():
    """Source-pin: the MCP panel JS reads r.data.skipped and renders
    each as an item with the reason."""
    js_path = (
        Path(mcp_client.__file__).parent.parent
        / "gateways" / "static" / "app.js"
    )
    src = js_path.read_text(encoding="utf-8")
    # Skipped variable extraction
    assert "r.data.skipped" in src
    # v1.31.14 marker
    assert "v1.31.14" in src
    # The empty-state condition now considers skipped too
    assert "servers.length === 0 && skipped.length === 0" in src
    # Footer reflects skipped count
    assert "skipped" in src.lower()


# -------------------- Version pin --------------------


def test_version_bumped_to_1_31_14_or_later():
    """v1.31.14 introduced the diagnostics + skipped-entry surface.
    Pin >= 1.31.14 instead of strict equality so subsequent point
    releases (v1.31.15, etc.) don't break this test — the v1.31.14
    machinery is what's being asserted, not the version number."""
    from janus import branding

    def _parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))

    assert _parts(branding.VERSION) >= (1, 31, 14)
