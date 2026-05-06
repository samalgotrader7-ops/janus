"""Tests for v1.22.0 web UI skeleton — static mount + panel API endpoints.

The pre-v1.22 web gateway served the chat UI as a 420-line inline HTML
string. v1.22.0 moves the frontend to janus/gateways/static/ and adds
read-only API endpoints for memory cards, skills, and workspace files
to power the new panel-based SPA.
"""
from __future__ import annotations

import pytest

try:
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    from janus.gateways import web_auth
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


pytestmark = pytest.mark.skipif(
    not _HAS_FASTAPI, reason="fastapi not installed",
)


def _authed_client(janus_home_path=None):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    c.csrf_token = r.json()["csrf_token"]  # type: ignore[attr-defined]
    return c


# ---------- static directory + assets ----------


def test_static_dir_exists():
    """The bundled frontend files must ship inside the package."""
    assert web_mod.STATIC_DIR.is_dir(), (
        "janus/gateways/static must exist — check pyproject package_data"
    )
    assert (web_mod.STATIC_DIR / "index.html").is_file()
    assert (web_mod.STATIC_DIR / "login.html").is_file()
    assert (web_mod.STATIC_DIR / "app.css").is_file()
    assert (web_mod.STATIC_DIR / "app.js").is_file()


def test_static_css_served(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/static/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers.get("content-type", "")
    assert ".panel" in r.text  # sanity check on body


def test_static_js_served(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/static/app.js")
    assert r.status_code == 200
    # FastAPI's StaticFiles uses application/javascript or text/javascript.
    ct = r.headers.get("content-type", "")
    assert "javascript" in ct.lower()
    assert "registerPanel" in r.text


def test_static_directory_traversal_blocked(janus_home):
    """StaticFiles must not allow ..-escapes out of the static dir."""
    c = _authed_client(janus_home)
    r = c.get("/static/../web.py")
    # FastAPI returns 404 for traversal attempts.
    assert r.status_code in (404, 403)


# ---------- inline HTML strings removed ----------


def test_inline_html_strings_removed():
    """v1.22.0 dropped _INDEX_HTML and _LOGIN_HTML inline strings.

    The frontend is canonical at janus/gateways/static/. Re-introducing
    inline strings would mean two copies that drift out of sync.
    """
    assert not hasattr(web_mod, "_INDEX_HTML"), (
        "_INDEX_HTML should have been removed in v1.22.0 — frontend "
        "lives in janus/gateways/static/index.html"
    )
    assert not hasattr(web_mod, "_LOGIN_HTML"), (
        "_LOGIN_HTML should have been removed in v1.22.0"
    )


# ---------- index page from static ----------


def test_index_renders_from_static_template(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text
    # New SPA shell markers.
    assert 'id="app"' in body
    assert "panel-chat" in body
    assert "panel-memory" in body
    assert "panel-skills" in body
    assert "panel-files" in body
    # Linked from /static.
    assert '/static/app.css' in body
    assert '/static/app.js' in body
    # CSRF token still embedded.
    assert 'name="csrf-token"' in body
    # No __PLACEHOLDER__ tokens leaked through.
    assert "__VERSION__" not in body
    assert "__CSRF_TOKEN__" not in body
    assert "__MODE__" not in body


def test_login_renders_from_static_template(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/login")
    assert r.status_code == 200
    body = r.text
    assert "<form" in body.lower()
    assert "/static/app.css" in body  # uses shared CSS
    assert "__ERROR_BLOCK__" not in body
    assert "__LOGO_SVG__" not in body


# ---------- /api/cards ----------


def test_api_cards_requires_auth(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/api/cards")
    assert r.status_code == 401


def test_api_cards_returns_list_when_empty(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/cards")
    assert r.status_code == 200
    data = r.json()
    assert "cards" in data
    assert isinstance(data["cards"], list)


def test_api_cards_filter_by_type(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/cards?type=identity")
    assert r.status_code == 200
    data = r.json()
    # Every returned card must match the filter (or list is empty).
    for c_obj in data.get("cards", []):
        assert c_obj["type"] == "identity"


# ---------- /api/skills ----------


def test_api_skills_requires_auth(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/api/skills")
    assert r.status_code == 401


def test_api_skills_returns_list(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/skills")
    assert r.status_code == 200
    data = r.json()
    assert "skills" in data
    assert isinstance(data["skills"], list)
    # If any skills are listed, each entry has the documented shape.
    for s in data["skills"]:
        assert "name" in s
        assert "version" in s
        assert "description" in s
        assert "state" in s


# ---------- /api/files ----------


def test_api_files_requires_auth(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/api/files")
    assert r.status_code == 401


def test_api_files_lists_workspace_root(janus_home, tmp_path, monkeypatch):
    """List the workspace root directory."""
    # Set workspace to a tmp dir with known content.
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "nested.md").write_text("nested", encoding="utf-8")

    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)

    c = _authed_client(janus_home)
    r = c.get("/api/files")
    assert r.status_code == 200
    data = r.json()
    names = sorted(e["name"] for e in data["entries"])
    assert "alpha.txt" in names
    assert "beta" in names
    # Dirs sort first.
    first = data["entries"][0]
    assert first["is_dir"] is True


def test_api_files_subdirectory(janus_home, tmp_path, monkeypatch):
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "file.md").write_text("x", encoding="utf-8")
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.get("/api/files?path=subdir")
    assert r.status_code == 200
    data = r.json()
    names = [e["name"] for e in data["entries"]]
    assert "file.md" in names


def test_api_files_blocks_traversal(janus_home, tmp_path, monkeypatch):
    """Path containing ../ that escapes workspace must be refused."""
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.get("/api/files?path=../..")
    # Either 400 (resolve_within rejected) — never 200.
    assert r.status_code == 400


def test_api_files_dotfiles_hidden(janus_home, tmp_path, monkeypatch):
    """Dotfiles are hidden by default in v1.22.0."""
    (tmp_path / "visible.txt").write_text("v", encoding="utf-8")
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.get("/api/files")
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["entries"]]
    assert "visible.txt" in names
    assert ".hidden" not in names


# ---------- /api/files/read ----------


def test_api_files_read_requires_auth(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/api/files/read?path=anything")
    assert r.status_code == 401


def test_api_files_read_returns_content(janus_home, tmp_path, monkeypatch):
    target = tmp_path / "hello.md"
    # Bytes-mode write so we control exact on-disk size (Windows
    # text-mode write() converts \n to \r\n and skews len vs size).
    target.write_bytes(b"# hello world\n")
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.get("/api/files/read?path=hello.md")
    assert r.status_code == 200
    data = r.json()
    assert data["content"] == "# hello world\n"
    assert data["size"] == 14
    assert data["path"] == "hello.md"


def test_api_files_read_refuses_path_outside_workspace(
    janus_home, tmp_path, monkeypatch,
):
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.get("/api/files/read?path=../../etc/passwd")
    assert r.status_code == 400


def test_api_files_read_refuses_huge_file(
    janus_home, tmp_path, monkeypatch,
):
    """v1.22.0 caps file reads at 1MB."""
    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"x" * (1_000_001))
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.get("/api/files/read?path=huge.bin")
    assert r.status_code == 413


def test_api_files_read_refuses_binary(
    janus_home, tmp_path, monkeypatch,
):
    """Binary files (non-UTF-8) are refused."""
    bin_file = tmp_path / "image.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03\xff\xfe\xfd")
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.get("/api/files/read?path=image.bin")
    assert r.status_code == 415
