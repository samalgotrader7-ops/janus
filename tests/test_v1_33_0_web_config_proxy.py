"""Tests for v1.33.0 — `janus web config <proxy>` (Phase 6.1).

WHAT THIS SHIPS:
A new `janus web config caddy|nginx` subcommand that emits a
ready-to-paste reverse-proxy snippet for the chosen proxy.
Auto-fills domain from JANUS_WEB_DOMAIN env or --domain flag.

INVARIANTS PINNED:
  * web_config module exports SUPPORTED_PROXIES = ('caddy', 'nginx')
  * resolve_params() returns (params, error) — error is non-None
    when no domain available
  * --domain flag overrides JANUS_WEB_DOMAIN env
  * Default upstream is 127.0.0.1; --host overrides
  * Default port is config.WEB_PORT (8765); --port overrides
  * render_caddy() output contains domain + reverse_proxy directive
    + X-Forwarded-* headers (so Phase 6.4 rate limiter sees real IPs)
  * render_nginx() output contains 80→443 redirect + ssl_certificate
    paths + proxy_pass + streaming-friendly proxy_buffering off
  * cmd_config() exits 2 on missing/invalid args, 0 on success
  * `janus web config <proxy>` is wired into __main__._run_web
"""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import web_config


# -------------------- Module surface --------------------


def test_supported_proxies_pinned():
    """The supported list is the exact set we ship templates for —
    if a future release adds Apache or HAProxy, this test bumps."""
    assert web_config.SUPPORTED_PROXIES == ("caddy", "nginx")


def test_proxy_params_dataclass_shape():
    p = web_config.ProxyParams(
        domain="janus.example.com",
        upstream_host="127.0.0.1",
        upstream_port=8765,
    )
    assert p.domain == "janus.example.com"
    assert p.upstream_host == "127.0.0.1"
    assert p.upstream_port == 8765


# -------------------- resolve_params --------------------


def test_resolve_params_uses_domain_arg():
    p, err = web_config.resolve_params(
        domain="janus.example.com",
        env={},
    )
    assert err is None
    assert p.domain == "janus.example.com"
    # Defaults
    assert p.upstream_host == "127.0.0.1"
    assert p.upstream_port == 8765


def test_resolve_params_uses_env_domain_when_no_arg():
    p, err = web_config.resolve_params(
        env={"JANUS_WEB_DOMAIN": "from-env.example.com"},
    )
    assert err is None
    assert p.domain == "from-env.example.com"


def test_resolve_params_arg_wins_over_env():
    p, err = web_config.resolve_params(
        domain="from-arg.com",
        env={"JANUS_WEB_DOMAIN": "from-env.com"},
    )
    assert err is None
    assert p.domain == "from-arg.com"


def test_resolve_params_errors_when_no_domain():
    p, err = web_config.resolve_params(env={})
    assert err is not None
    assert "domain" in err.lower()


def test_resolve_params_honors_host_override():
    p, err = web_config.resolve_params(
        domain="example.com",
        host="10.0.0.5",
        env={},
    )
    assert err is None
    assert p.upstream_host == "10.0.0.5"


def test_resolve_params_honors_port_override():
    p, err = web_config.resolve_params(
        domain="example.com",
        port=9000,
        env={},
    )
    assert err is None
    assert p.upstream_port == 9000


# -------------------- Caddy template --------------------


def test_render_caddy_contains_domain():
    p = web_config.ProxyParams("janus.example.com", "127.0.0.1", 8765)
    out = web_config.render_caddy(p)
    assert "janus.example.com" in out


def test_render_caddy_uses_reverse_proxy_directive():
    p = web_config.ProxyParams("ex.com", "127.0.0.1", 8765)
    out = web_config.render_caddy(p)
    assert "reverse_proxy 127.0.0.1:8765" in out


def test_render_caddy_forwards_real_ip_for_phase_6_4():
    """Rate limiter (Phase 6.4) needs to see real client IPs;
    the Caddy snippet must forward X-Real-IP / X-Forwarded-For."""
    p = web_config.ProxyParams("ex.com", "127.0.0.1", 8765)
    out = web_config.render_caddy(p)
    assert "X-Real-IP" in out
    assert "X-Forwarded-For" in out


def test_render_caddy_mentions_letsencrypt():
    """Caddy's selling point vs nginx for new users is automatic
    Let's Encrypt — the snippet should advertise that."""
    p = web_config.ProxyParams("ex.com", "127.0.0.1", 8765)
    out = web_config.render_caddy(p)
    assert "Let's Encrypt" in out or "auto" in out.lower()


# -------------------- nginx template --------------------


def test_render_nginx_has_80_to_443_redirect():
    """Modern default: HTTP requests redirect to HTTPS rather than
    serving plaintext."""
    p = web_config.ProxyParams("ex.com", "127.0.0.1", 8765)
    out = web_config.render_nginx(p)
    assert "listen 80" in out
    assert "return 301 https" in out


def test_render_nginx_has_ssl_cert_paths():
    """Cert paths assume Let's Encrypt via certbot — pin so a
    future edit doesn't break that assumption."""
    p = web_config.ProxyParams("ex.com", "127.0.0.1", 8765)
    out = web_config.render_nginx(p)
    assert "ssl_certificate" in out
    # Domain interpolated into the cert path
    assert "/etc/letsencrypt/live/ex.com" in out


def test_render_nginx_proxy_pass_uses_upstream():
    p = web_config.ProxyParams("ex.com", "10.0.0.5", 9000)
    out = web_config.render_nginx(p)
    assert "proxy_pass http://10.0.0.5:9000" in out


def test_render_nginx_streaming_friendly():
    """SSE streaming (event substrate) needs proxy_buffering off
    + a long read timeout — a tight default would close the
    connection mid-stream."""
    p = web_config.ProxyParams("ex.com", "127.0.0.1", 8765)
    out = web_config.render_nginx(p)
    assert "proxy_buffering off" in out
    # Read timeout > a few minutes (covers long-running tool calls)
    assert "proxy_read_timeout" in out


def test_render_nginx_forwards_real_ip_for_phase_6_4():
    p = web_config.ProxyParams("ex.com", "127.0.0.1", 8765)
    out = web_config.render_nginx(p)
    assert "X-Real-IP" in out
    assert "X-Forwarded-For" in out


# -------------------- Dispatch + CLI --------------------


def test_render_unknown_proxy_raises():
    p = web_config.ProxyParams("ex.com", "127.0.0.1", 8765)
    with pytest.raises(ValueError):
        web_config.render("apache", p)


def test_cmd_config_no_args_prints_usage(capsys):
    rc = web_config.cmd_config([])
    assert rc == 2
    captured = capsys.readouterr()
    assert "usage:" in captured.err.lower()


def test_cmd_config_unknown_proxy_errors(capsys):
    rc = web_config.cmd_config(["apache", "--domain", "ex.com"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown proxy" in captured.err.lower()


def test_cmd_config_missing_domain_errors(capsys, monkeypatch):
    monkeypatch.delenv("JANUS_WEB_DOMAIN", raising=False)
    rc = web_config.cmd_config(["caddy"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "domain" in captured.err.lower()


def test_cmd_config_caddy_emits_to_stdout(capsys):
    rc = web_config.cmd_config(["caddy", "--domain", "ex.com"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "ex.com" in captured.out
    assert "reverse_proxy" in captured.out
    # No spurious stderr on success
    assert captured.err == ""


def test_cmd_config_nginx_emits_to_stdout(capsys):
    rc = web_config.cmd_config(["nginx", "--domain", "ex.com"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "ex.com" in captured.out
    assert "ssl_certificate" in captured.out


def test_cmd_config_invalid_port_errors(capsys):
    rc = web_config.cmd_config(["caddy", "--domain", "ex.com", "--port", "abc"])
    assert rc == 2


def test_cmd_config_unknown_flag_errors(capsys):
    rc = web_config.cmd_config(["caddy", "--domain", "ex.com", "--bogus"])
    assert rc == 2


# -------------------- __main__ wiring --------------------


def test_main_run_web_dispatches_config_subcommand():
    """Source-pin: __main__._run_web routes 'config' to web_config.cmd_config."""
    main_path = Path(web_config.__file__).parent / "__main__.py"
    src = main_path.read_text(encoding="utf-8")
    run_web_idx = src.index("def _run_web(")
    block = src[run_web_idx: run_web_idx + 1500]
    assert 'args[0] == "config"' in block
    assert "from .web_config import cmd_config" in block


# -------------------- Version pin --------------------


def test_version_bumped_to_1_33_0_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 33, 0)
