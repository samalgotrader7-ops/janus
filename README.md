<div align="center">

# Janus

**Claude Code's UX, on any model, with plain-text state and a learning loop.**

</div>

Janus is a local AI agent that talks to you the way Claude Code does —
streaming responses, inline tool calls, permission modes you set and
forget — but runs against any OpenAI-compatible model (Anthropic,
OpenRouter, Ollama, OpenAI, llama.cpp, …). Memory, skills, hooks, and
every interaction live as plain-text files under `~/.janus/` that you
can `cat`, `grep`, version-control, or hand-edit.

What makes it useful:

- **Model-agnostic.** Swap providers with one env var. No vendor lock-in.
- **Plain-text everywhere.** Memory is markdown, conversations are JSON,
  the audit log is JSONL. No opaque database.
- **Skills you teach it.** Durable workflows land as `quarantined`
  markdown files; you `/promote` them to trusted state when ready.
- **Permission modes copied from Claude Code.** `default` /
  `acceptEdits` / `plan` / `bypassPermissions`, switchable mid-session
  with `/mode`.
- **Self-hostable.** No cloud dependencies. No SaaS user model.

---

## 60-second quickstart

```bash
# 1. Install (Linux / macOS — requires Python 3.10+)
curl -sSL https://raw.githubusercontent.com/samalgotrader7-ops/janus/main/scripts/install.sh | sh

# 2. Configure (interactive — picks up existing OPENAI_API_KEY etc.)
janus onboard

# 3. Chat
janus
```

That's it. You'll see a streaming chat with inline tool calls and a
permission prompt for any side effect.

> **Want a deeper walkthrough?** Four progressive tutorials in
> [`docs/tutorials/`](docs/tutorials/) cover skills, memory, and MCP
> integration — each one screenful, ~10 minutes each.

> **Want to deploy on a VPS?**
> [`scripts/install_services.sh`](scripts/install_services.sh) installs
> systemd units for `telegram` + `web` + `daemon` with auto-restart on
> `git pull`. See [Production deployment](#production-deployment) below.

---

## Table of contents

- [Why Janus](#why-janus)
- [Install](#install)
- [Configure](#configure)
- [Quickstart](#quickstart)
- [Tutorials](#tutorials)
- [Production deployment](#production-deployment)
- [Permission modes](#permission-modes)
- [Updating](#updating)
- [Slash commands](#slash-commands)
- [Architecture](#architecture)
- [Where state lives](#where-state-lives)
- [Testing](#testing)
- [Documentation](#documentation)
- [License](#license)

---

## Why Janus

| | Janus | Claude Code | Typical agent CLI |
|---|---|---|---|
| **Model** | Any OpenAI-compatible | Anthropic only | Provider-locked |
| **State** | Plain text under `~/.janus/` (cat, grep, git) | Opaque | Database-backed |
| **Permission modes** | `default` / `acceptEdits` / `plan` / `bypass` | Same | Coarse "yolo / ask" |
| **Skills** | Markdown files; quarantined → user `/promote`s | None first-class | Auto-evolve, hard to roll back |
| **Memory** | Plain `user.md` + diff propose + manual approve | None first-class | Opaque embeddings |
| **Logs** | Every turn → `~/.janus/log.jsonl`, FTS5-indexed | n/a | Best-effort |
| **Model-call path** | ~50 lines of `requests`, no SDK | n/a | litellm / openai-python |
| **Self-hostable** | Yes | No | Varies |

If you want Claude Code's ergonomics without Anthropic lock-in, with
state you can audit and a skill system you actually control, Janus is
for you.

---

## Install

Janus runs on **Windows, macOS, and Linux** with Python ≥ 3.10. The core
install is one dependency (`requests`) — everything else is opt-in.

### Easiest — one-line installer (Linux / macOS)

```bash
curl -sSL https://raw.githubusercontent.com/samalgotrader7-ops/janus/main/scripts/install.sh | sh
```

Detects your platform, installs `pipx` if missing, pulls Janus from
PyPI (or git+URL fallback), and prints next-step instructions. Handles
PEP 668 (`--break-system-packages`) on recent Debian / Ubuntu.

### Docker (any platform)

```bash
# Standalone
docker run --rm -it -p 8765:8765 \
  -v janus-data:/root/.janus \
  --env-file .env \
  ghcr.io/samalgotrader7-ops/janus:latest web

# Three-service stack (web + telegram + daemon)
git clone https://github.com/samalgotrader7-ops/janus.git
cd janus
cp .env.example .env  # fill in your keys
docker compose up -d
```

### From source (for contributors)

#### 1. Clone

```bash
git clone https://github.com/samalgotrader7-ops/janus.git
cd janus
```

#### 2. Create a virtual environment

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
</details>

<details>
<summary><b>macOS / Linux</b></summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
```
</details>

#### 3. Install

##### Option A — pipx (recommended on Linux & macOS)

`pipx` puts `janus` on your PATH globally without needing to activate a
venv each shell. This is the durable install for servers and dev boxes.

```bash
sudo apt install -y pipx          # Ubuntu / Debian
# or: brew install pipx           # macOS
pipx ensurepath                    # adds ~/.local/bin to PATH (once)

pipx install -e ".[rich]"          # editable install + rich TUI
```

To later switch extras: `pipx uninstall janus-agent && pipx install -e ".[all]"`.

##### Option B — pip in a venv (works everywhere)

```bash
pip install -e .                   # core only (just `requests`)
pip install -e ".[rich]"           # + polished TUI (recommended)
pip install -e ".[web]"            # + local web UI
pip install -e ".[browser]"        # + headless Chromium tools
pip install -e ".[all]"            # everything
```

After install, `janus` is on your PATH **as long as the venv is
activated**. Open a new shell and you'll need to `source .venv/bin/activate`
again, or use Option A above.

#### 4. Smoke test

```bash
janus --version     # janus 1.1
janus --logo        # prints the bifurcation logo
janus --help        # subcommands and flags
janus --doctor      # config + environment diagnostics
```

---

## Configure

Copy the example env file and fill in your provider key:

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
Copy-Item .env.example .env
notepad .env
```
</details>

<details>
<summary><b>macOS / Linux</b></summary>

```bash
cp .env.example .env
$EDITOR .env
```
</details>

Janus auto-loads `.env` from the current working directory, then falls
back to `~/.janus/.env`. Existing shell exports always win.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `JANUS_API_KEY` | ✅ | — | Provider key (OpenRouter, Anthropic, OpenAI, …) |
| `JANUS_API_BASE` |   | `https://openrouter.ai/api/v1` | Override for direct providers |
| `JANUS_MODEL` |   | `openai/gpt-4o-mini` | Any chat model the base supports |
| `JANUS_WORKSPACE` |   | current directory | Pin so fs/shell tools can't escape |
| `JANUS_MEMORY_MODEL` |   | same as `JANUS_MODEL` | Cheaper model for the memory-diff loop |
| `JANUS_BRAVE_API_KEY` |   | — | Web search (free key at brave.com) |
| `JANUS_TELEGRAM_TOKEN` |   | — | Telegram gateway |
| `JANUS_WHATSAPP_*` |   | — | WhatsApp Cloud API webhook |

Full reference: [`.env.example`](.env.example).

---

## Quickstart

```bash
janus                              # interactive REPL (rich TUI if installed)
janus --basic                      # plain input() loop
janus -p "summarise the readme"    # headless, single-shot

janus web                          # local web UI on http://127.0.0.1:8765
janus daemon                       # proactive trigger daemon
janus telegram                     # Telegram bot
janus whatsapp                     # WhatsApp webhook

janus --eval --last 20             # replay recent turns at temp=0
janus --analyze                    # log statistics
janus --resume <id>                # resume a saved conversation
janus --continue                   # continue the most recent one
```

A typical session:

```
›  refactor janus/cli_rich.py to extract the dispatcher into its own module

I'll start by reading the current cli_rich.py to see what's in the
dispatcher.

   → fs_read(path=janus/cli_rich.py)
   ✓ 1209 lines

The dispatcher is roughly 280 lines (_dispatch + helpers). I'll move it to
janus/dispatcher.py and re-export. Approve the new file?

   ⚠ approval needed: fs_write: create  (risk=write, mode=default)
   create janus/dispatcher.py  (8412 bytes)
   --- proposed contents ---
   ...

approve? [y/N]: y
```

No interpretation picker — just chat with tool calls inline, gated by
the active permission mode. Use `/why` if you want the model to surface
2–3 alternative readings of a message before acting.

---

## Tutorials

Four progressive walkthroughs in [`docs/tutorials/`](docs/tutorials/),
each one screenful (~10 minutes):

1. **[Hello, Janus](docs/tutorials/01-hello-janus.md)** — install,
   configure a model, first turn.
2. **[Your First Skill](docs/tutorials/02-your-first-skill.md)** —
   write a skill, see it auto-load, `/promote` it.
3. **[Memory Loop](docs/tutorials/03-memory-loop.md)** — memory
   proposals, review, hygiene.
4. **[Connect MCP](docs/tutorials/04-connect-mcp.md)** — configure a
   stdio MCP server, list tools, call them through Janus.

---

## Production deployment

For VPS / always-on deployments, the **systemd path** is recommended:

```bash
# On the VPS (after one-line install):
git clone https://github.com/samalgotrader7-ops/janus.git /opt/janus
cd /opt/janus

# Set required env vars in your shell, then:
bash scripts/install_services.sh
```

What this gets you:

- Three systemd user-units: `janus-telegram`, `janus-web`,
  `janus-daemon` — each with auto-restart on failure.
- `~/.janus/.env` written with `chmod 600` from your shell env.
- `loginctl enable-linger` so units survive SSH logout.
- `git config core.hooksPath scripts/git-hooks` so `git pull` auto-
  restarts services when `janus/*.py` changes.
- Bypass with `JANUS_NO_AUTO_RESTART=1 git pull`.

For Docker, see `docker-compose.yml` (three services share one named
volume `janus-data`).

---

## Permission modes

Copied from Claude Code so muscle memory transfers. Switch with
`/mode <name>` or set `JANUS_APPROVAL` in `.env`. The decision matrix:

| mode | read | write | exec |
|---|---|---|---|
| `default` | allow | ask | ask |
| `acceptEdits` | allow | allow | ask |
| `plan` | allow | **DENY** | **DENY** |
| `bypassPermissions` | allow | allow | allow |

- **default** — start here. The model can read freely, asks before
  writing files or running commands.
- **acceptEdits** — for trusted refactors when you don't want to babysit
  every diff. Shell commands still ask.
- **plan** — read-only thinking. The model can browse the codebase and
  propose a plan; nothing mutates. Switch to `default` when ready to act.
- **bypassPermissions** — fully autonomous. Use only in throwaway
  workspaces.

Skills can grant capability tokens (e.g. `shell.exec: ["git *"]`) that
short-circuit the prompt for narrow targets without flipping the whole
session into bypass.

---

## Updating

Janus follows simple SemVer tags (`v0.13.0`, `v0.14.0`, …). Pull the
latest tag and reinstall in place:

```bash
cd /path/to/janus
git pull --tags
janus --version            # confirm the new version
```

Then **reinstall** so the new code is picked up:

```bash
# If installed via pipx:
pipx reinstall janus-agent

# If installed via pip in a venv:
source .venv/bin/activate
pip install -e ".[rich]" --upgrade
```

To pin to a specific release:

```bash
git checkout v0.13.0
pipx reinstall janus-agent      # or: pip install -e ".[rich]" --upgrade
```

---

## Slash commands

Type `/` in the REPL to open the dropdown. All commands carry inline
descriptions; the menu is grouped by source (built-in vs. user-defined).

| Command | Purpose |
|---|---|
| `/mode [name]` | Switch permission mode: `default` / `acceptEdits` / `plan` / `bypassPermissions` |
| `/why` | Re-interpret your last message and show 2–3 candidate readings |
| `/workspace [path]` | Show or change the active workspace directory |
| `/analyze` | Scan the workspace for tools, skills, project hints |
| `/memory` | Show the `user.md` memory file |
| `/search <query>` | Search prior interactions in the FTS5 log index |
| `/skills` | List installed skills with state and trust score |
| `/promote <name> <state>` | Promote a quarantined skill to a trusted state |
| `/skill new \| review \| import` | Skill authoring |
| `/cost` | Token + cost summary for this session |
| `/clear` | Clear conversation turns and cost counters |
| `/compact` | Summarize and prune older turns |
| `/resume <id>` / `/continue` | Conversation continuity |
| `/verbose` / `/stream` | Toggle verbose tool args / token streaming |
| `/init` | Scan codebase and propose starter `user.md` + skills |
| `/model [id]` | Show or set the model for this session |
| `/doctor` | Run diagnostics on configuration and environment |
| `/output-style` | Switch output rendering (markdown / plain / json / …) |
| `/commands` | List user-defined slash commands |
| `/eval [--last N] [--skill <name>]` | Replay last N turns at temp=0 (drift check) |
| `/mcp list \| connect \| disconnect` | Manage MCP servers |
| `/triggers` | List configured triggers |
| `/help` | Full grouped command list |
| `/quit` | Exit |

You can author your own commands as plain markdown files with optional
frontmatter — drop them in `~/.janus/commands/` (global) or
`<workspace>/.janus/commands/` (per-project):

```markdown
---
name: refactor
description: rewrite the supplied snippet for clarity
---

Refactor this code:

{args}
```

---

## Architecture

```
user types
   │
   ▼
slash command? handle and continue
   │
   ▼
skills.match()  →  trusted-auto skill attaches (if any)
   │
   ▼
executor.chat(messages, user_input, tools, approver, mode, …)
   │
   ▼
loop:
   llm.chat_stream(messages, tools=registry.schemas())
   if tool_calls:
      for each call:
         hooks.fire(PreToolUse) → maybe deny
         tools.call() → approver(action, details, risk=…, capability=(…))
                       → permissions.decide(risk, mode) → allow / ask / deny
         hooks.fire(PostToolUse)
         append tool result
   else:
      final text → return
   │
   ▼
cost.record()  ·  conversation.add_turn()  ·  skill.record_run()
memory.propose_diff()  ·  logger.write()
```

The legacy interpretation-gated flow is preserved on `/why` for users
who want to inspect ambiguity before acting.

---

## Where state lives

Everything Janus learns about you lives under `~/.janus/`, in plain
files you can open in any text editor:

```
~/.janus/
├── log.jsonl                   every interaction (append-only)
├── sessions.db                 SQLite FTS5 index over log.jsonl
├── user.md                     plain-text user model
├── skills/<name>.md            installed skills
├── conversations/<id>.json     resumable conversations
├── triggers/<name>.yaml        proactive triggers
├── hooks/<event>.<order>.json  lifecycle hooks
├── mcp/servers.json            MCP server registry
├── commands/<name>.md          custom slash commands
├── evals/run-<ts>.json         eval reports
└── demo/phase_<N>.md           per-phase demo records
```

Inspect, diff, version-control, or hand-edit any of it. There is no
opaque database.

---

## Testing

```bash
pip install -e ".[test]"
pytest tests/ -q
```

The suite is fast (~12 s for 401 tests as of v1.1) and uses no network
or LLM calls — `fake_llm` and `janus_home` fixtures isolate every run.

---

## Documentation

| Doc | What it covers |
|---|---|
| [`docs/JANUS_MASTER_SPEC.md`](docs/JANUS_MASTER_SPEC.md) | The design contract |
| [`docs/BUILD_GUIDE_FOR_CLAUDE_CODE.md`](docs/BUILD_GUIDE_FOR_CLAUDE_CODE.md) | The operational contract — read before contributing |
| [`docs/HERMES_AUDIT.md`](docs/HERMES_AUDIT.md) | Competitive thesis + why each safety primitive exists |
| [`docs/PHASE_2_3_DESIGN.md`](docs/PHASE_2_3_DESIGN.md) | Original design rationale for memory + skills |
| [`CLAUDE.md`](CLAUDE.md) | Onboarding for AI assistants working in this repo |

---

## License

[MIT](LICENSE) © 2026 Sam.
