---
name: environment-probe
description: Auto-discover the env — installed CLIs, MCP servers, reachable APIs, configured providers.
state: quarantined
capabilities:
  shell.exec:
    - "which *"
    - "command -v *"
    - "where *"
    - "*.exe --version"
    - "*.exe -V"
    - "* --version"
    - "* -V"
    - "git --version"
    - "node --version"
    - "python --version"
    - "docker --version"
    - "gh --version"
    - "kubectl version*"
    - "aws --version"
  fs.read:
    - "~/.janus/**"
    - "~/.claude/settings.json"
    - "~/.aws/config"
    - "~/.kube/config"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running environment-probe.

You enumerate what's available in this environment so the rest of the
session knows what it can call. Skills/tools fail silently when
prerequisites are missing; this skill catches it once at the start.

Steps:
1. CLI INVENTORY: probe known relevant binaries (git, gh, docker, kubectl,
   aws, gcloud, node, python, ffmpeg, yt-dlp, himalaya, manim, etc.).
   For each, capture version + install path. Don't try to install
   anything; observe only.
2. JANUS STATE: read `~/.janus/skills/` (count + states), `mcp/servers.json`
   (configured servers), `hooks.json` (hooks count), `.env` (which
   provider keys are set — names only, not values).
3. CLAUDE INTEROP: check `~/.claude/settings.json` for shared MCP /
   permissions config.
4. CLOUD PROFILES: parse `~/.aws/config`, `~/.kube/config`, `~/.gcp/*`
   for available profiles (names only). Don't probe the cloud APIs
   unless the user asks.
5. NETWORK REACHABILITY: a small set of well-known endpoints
   (api.openai.com, api.anthropic.com, openrouter.ai, github.com)
   reached via web.fetch (HEAD / OPTIONS, not full GET) to confirm
   outbound connectivity. Read-only probes only.
6. REPORT a single-page capability map. Group by category. Flag what's
   MISSING that this user's typical tasks need (cross-reference
   `~/.janus/log.jsonl` for the most-used tools).

Read-only. Don't install. Don't authenticate. Don't store probe
results in any file the user didn't ask for. The output is the
report; the side effects are zero.
