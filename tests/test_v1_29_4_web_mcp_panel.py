"""Tests for v1.29.4 — web MCP catalog panel.

Adds a new MCP browser panel to the web SPA shell that consumes
the v1.29.1 ``/api/mcp/catalog`` endpoint. Read-only — connect /
disconnect remain CLI operations (web has no auth-gated mutation
flow for MCP yet, and the connect step spawns a subprocess we'd
want better cleanup semantics for before exposing on the web).
"""

from __future__ import annotations

from pathlib import Path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ============================================================
# Static asset additions
# ============================================================


def test_index_html_has_mcp_nav_entry():
    """Sidebar nav lists the MCP panel between cost and settings."""
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    html = _read(static / "index.html")
    assert 'data-panel="mcp"' in html
    assert 'href="#mcp"' in html


def test_index_html_has_mcp_panel_section():
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    html = _read(static / "index.html")
    assert 'id="panel-mcp"' in html
    # Panel mounts the list + refresh button the JS expects
    assert 'id="mcp-list"' in html
    assert 'id="mcp-refresh"' in html


def test_index_html_mcp_panel_says_read_only():
    """v1.29.4 explicitly does not mutate — the panel UI must say so."""
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    html = _read(static / "index.html")
    # Find the MCP panel section
    start = html.find('id="panel-mcp"')
    end = html.find("</section>", start)
    section = html[start:end]
    assert "browse-only" in section.lower() or "read-only" in section.lower()


def test_app_js_registers_mcp_panel():
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    js = _read(static / "app.js")
    assert "registerPanel('mcp'" in js


def test_app_js_mcp_panel_calls_catalog_endpoint():
    """The panel's load fetches /api/mcp/catalog."""
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    js = _read(static / "app.js")
    # Find the MCP panel block
    start = js.find("registerPanel('mcp'")
    assert start > -1
    # Span the panel body — next registerPanel or end
    next_panel = js.find("registerPanel(", start + 30)
    if next_panel == -1:
        next_panel = start + 4000
    block = js[start:next_panel]
    assert "/api/mcp/catalog" in block


def test_app_js_mcp_panel_handles_error_field():
    """The panel must show server error responses (e.g.
    list_tools failed) without crashing."""
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    js = _read(static / "app.js")
    start = js.find("registerPanel('mcp'")
    next_panel = js.find("registerPanel(", start + 30)
    if next_panel == -1:
        next_panel = start + 4000
    block = js[start:next_panel]
    # Top-level r.data.error path
    assert "r.data.error" in block
    # Per-server s.error field path
    assert "s.error" in block


def test_app_js_mcp_panel_renders_tool_inventory():
    """Connected servers should show their tools with description +
    param count + janus_name."""
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    js = _read(static / "app.js")
    start = js.find("registerPanel('mcp'")
    next_panel = js.find("registerPanel(", start + 30)
    if next_panel == -1:
        next_panel = start + 4000
    block = js[start:next_panel]
    assert "s.tools" in block
    assert "param_count" in block
    assert "janus_name" in block
    # Description shown
    assert "t.description" in block


def test_app_js_mcp_panel_distinguishes_connected_state():
    """Connected vs. configured-only servers should render
    differently — typically with the existing 'promoted' /
    'quarantined' tag classes."""
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    js = _read(static / "app.js")
    start = js.find("registerPanel('mcp'")
    next_panel = js.find("registerPanel(", start + 30)
    if next_panel == -1:
        next_panel = start + 4000
    block = js[start:next_panel]
    assert "s.connected" in block
    # Visual differentiation via tag class
    assert "promoted" in block or "tag connected" in block
    assert "quarantined" in block or "tag configured" in block


def test_app_js_mcp_panel_handles_empty_servers():
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    js = _read(static / "app.js")
    start = js.find("registerPanel('mcp'")
    next_panel = js.find("registerPanel(", start + 30)
    if next_panel == -1:
        next_panel = start + 4000
    block = js[start:next_panel]
    assert "no MCP servers" in block.lower() or "servers.length === 0" in block


def test_app_js_mcp_panel_sets_footer_count():
    """The footer status bar should show server + connected counts."""
    static = Path(__file__).resolve().parent.parent / "janus" / "gateways" / "static"
    js = _read(static / "app.js")
    start = js.find("registerPanel('mcp'")
    next_panel = js.find("registerPanel(", start + 30)
    if next_panel == -1:
        next_panel = start + 4000
    block = js[start:next_panel]
    assert "setFooter" in block
    # Counts both totals and connected
    assert "liveCount" in block or "connected" in block.lower()
