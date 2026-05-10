"""
web_config.py — reverse-proxy config snippet generator (v1.33.0).

WHY THIS EXISTS:
Phase 6 / Production hardening. Janus's web UI binds 127.0.0.1
by default and speaks plain HTTP — by design, since putting TLS
termination INSIDE the agent process would mean re-implementing
Caddy / nginx badly. The recommended deployment pattern is to put
a real TLS-terminating reverse proxy in front of janus-web.

Pre-v1.33 a user who wanted to publish their Janus to the internet
had to look up Caddy / nginx config syntax themselves. v1.33.0
ships `janus web config <proxy>` which emits a ready-to-paste
config snippet for the chosen proxy.

USAGE:
  janus web config caddy                    # auto-detect domain
  JANUS_WEB_DOMAIN=janus.example.com janus web config caddy
  janus web config nginx --domain my.host
  janus web config caddy --port 8765        # override port

OUTPUTS to stdout — pipe into your config:
  janus web config caddy >> /etc/caddy/Caddyfile

PROXIES SUPPORTED:
  - caddy (recommended — auto-TLS via Let's Encrypt out of the box)
  - nginx (classic; user supplies cert paths)

What we deliberately DON'T do:
  - Auto-install / restart the proxy. That's the sysadmin's call.
  - Manage cert files. Caddy handles auto-TLS; nginx users have
    their own cert management.
  - Generate full standalone configs. The output is a snippet
    intended for inclusion in an existing Caddyfile / nginx.conf.

P5 (plain-text state): the user pastes the output, can edit it
freely, can version-control it. We don't write anywhere.
"""

from __future__ import annotations
import os
import sys
from dataclasses import dataclass

from . import config


SUPPORTED_PROXIES = ("caddy", "nginx")


@dataclass(frozen=True)
class ProxyParams:
    """Parameters consumed by the proxy config templates."""

    domain: str
    upstream_host: str
    upstream_port: int


def resolve_params(
    *,
    domain: str | None = None,
    port: int | None = None,
    host: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[ProxyParams, str | None]:
    """Resolve domain + upstream from CLI args / env / defaults.

    Returns (params, error_message). When error_message is not None
    the caller should print it and exit non-zero.
    """
    if env is None:
        env = dict(os.environ)
    chosen_domain = (
        domain
        or env.get("JANUS_WEB_DOMAIN")
        or ""
    ).strip()
    if not chosen_domain:
        return (
            ProxyParams(domain="", upstream_host="", upstream_port=0),
            "no domain specified — pass --domain <host> or "
            "set JANUS_WEB_DOMAIN in your env",
        )
    # Upstream host: where the proxy connects to find janus-web.
    # When the proxy runs on the SAME host as janus-web (typical),
    # this is 127.0.0.1; otherwise the user can override.
    chosen_host = (host or env.get("JANUS_WEB_UPSTREAM_HOST") or "127.0.0.1").strip()
    chosen_port = port or int(
        env.get("JANUS_WEB_PORT") or getattr(config, "WEB_PORT", 8765)
    )
    return (
        ProxyParams(
            domain=chosen_domain,
            upstream_host=chosen_host,
            upstream_port=chosen_port,
        ),
        None,
    )


# ---------- Caddy template ----------


_CADDY_TEMPLATE = """\
# Janus reverse proxy — paste into your Caddyfile.
# Caddy auto-provisions Let's Encrypt certs for the domain.
# After editing: sudo systemctl reload caddy
{domain} {{
    reverse_proxy {upstream_host}:{upstream_port}

    # Forward client IP for janus-web's rate limiter (Phase 6.4)
    # to see real client addresses instead of the proxy's loopback.
    header_up X-Real-IP {{remote_host}}
    header_up X-Forwarded-For {{remote_host}}
    header_up X-Forwarded-Proto {{scheme}}

    # Recommended: enable HTTP/2 + compression (Caddy defaults are sane).
    encode zstd gzip

    # Hide the upstream from error pages.
    handle_errors {{
        respond "Janus is temporarily unavailable. Try again in a moment."
    }}
}}
"""


def render_caddy(p: ProxyParams) -> str:
    return _CADDY_TEMPLATE.format(
        domain=p.domain,
        upstream_host=p.upstream_host,
        upstream_port=p.upstream_port,
    )


# ---------- nginx template ----------


_NGINX_TEMPLATE = """\
# Janus reverse proxy — paste into your nginx site config
# (e.g., /etc/nginx/sites-available/janus → symlink in sites-enabled/).
# Cert paths assume Let's Encrypt via certbot --nginx; adjust if you
# use a different cert source. After editing: sudo nginx -t && sudo
# systemctl reload nginx.

server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    # Redirect everything to HTTPS.
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};

    # Cert paths — adjust to where your certs live.
    ssl_certificate     /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

    # Modern TLS defaults; tweak as needed for your cipher policy.
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    # Body size for any future file uploads through the chat surface.
    client_max_body_size 25m;

    location / {{
        proxy_pass http://{upstream_host}:{upstream_port};

        # Forward client identifying headers — Phase 6.4 rate limiter
        # uses these to identify real clients vs proxy loopback.
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Streaming + SSE friendliness — janus-web's event stream
        # (cli_rich-equivalent live tool calls) needs flushed output.
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}
}}
"""


def render_nginx(p: ProxyParams) -> str:
    return _NGINX_TEMPLATE.format(
        domain=p.domain,
        upstream_host=p.upstream_host,
        upstream_port=p.upstream_port,
    )


# ---------- Dispatch ----------


def render(proxy: str, params: ProxyParams) -> str:
    if proxy == "caddy":
        return render_caddy(params)
    if proxy == "nginx":
        return render_nginx(params)
    raise ValueError(f"unknown proxy {proxy!r}; supported: {SUPPORTED_PROXIES}")


def cmd_config(args: list[str]) -> int:
    """`janus web config <proxy> [--domain X] [--host Y] [--port N]` —
    emit the snippet to stdout. Exit code 0 on success, 2 on usage
    error.
    """
    if not args:
        sys.stderr.write(
            "usage: janus web config <proxy> [--domain HOST] "
            "[--host UPSTREAM] [--port PORT]\n"
        )
        sys.stderr.write(f"  supported proxies: {', '.join(SUPPORTED_PROXIES)}\n")
        return 2
    proxy = args[0].lower()
    if proxy not in SUPPORTED_PROXIES:
        sys.stderr.write(
            f"error: unknown proxy {args[0]!r}; "
            f"supported: {', '.join(SUPPORTED_PROXIES)}\n"
        )
        return 2

    domain = None
    upstream_host = None
    port = None
    rest = args[1:]
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag == "--domain":
            try:
                domain = rest[i + 1]
                i += 2
            except IndexError:
                sys.stderr.write("error: --domain requires a value\n")
                return 2
        elif flag == "--host":
            try:
                upstream_host = rest[i + 1]
                i += 2
            except IndexError:
                sys.stderr.write("error: --host requires a value\n")
                return 2
        elif flag == "--port":
            try:
                port = int(rest[i + 1])
                i += 2
            except (IndexError, ValueError):
                sys.stderr.write("error: --port requires an integer\n")
                return 2
        else:
            sys.stderr.write(f"error: unknown flag {flag!r}\n")
            return 2

    params, err = resolve_params(domain=domain, port=port, host=upstream_host)
    if err:
        sys.stderr.write(f"error: {err}\n")
        return 2
    sys.stdout.write(render(proxy, params))
    return 0
