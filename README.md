<div align="center">

# Janus

**An intent-first, safety-first, self-improving local AI agent framework.**

</div>

Janus reads every request you give it, proposes 2–3 candidate
interpretations, and waits for you to pick one before any tool fires. It
runs the chosen interpretation through a small hardened tool set gated
by capability tokens and explicit approvals, remembers what matters in a
plain-text user model you can edit, learns durable workflows as
**skills** the user explicitly promotes, and decomposes complex work
into sub-tasks executed by parallel sub-agents — all locally, all in
files you can read.

The competitive thesis lives in [`docs/HERMES_AUDIT.md`](docs/HERMES_AUDIT.md):
agentic frameworks today optimize for autonomy at the cost of
structural safety. Janus's lane is the same self-improving substrate
with structural safety as the **default** rather than an opt-in.

---

## Table of contents

- [Why Janus](#why-janus)
- [Install](#install)
- [Configure](#configure)
- [Quickstart](#quickstart)
- [Updating](#updating)
- [Slash commands](#slash-commands)
- [Architecture](#architecture)
- [Where state lives](#where-state-lives)
- [Safety invariants (P1–P10)](#safety-invariants-p1p10)
- [Testing](#testing)
- [Documentation](#documentation)
- [License](#license)

---

## Why Janus

| | Janus | Typical agent CLI |
|---|---|---|
| **Interpretation gate** | 2–3 candidates surfaced before any tool runs | Tool fires on the first plausible plan |
| **Capability tokens** | Each dangerous action is bounded by a token + y/N | Coarse "yolo / ask" toggle |
| **Skills** | Land as `quarantined`; only the user can `/promote` to `trusted-auto` | Auto-evolve, hard to roll back |
| **Memory** | Plain-text `user.md` + diff propose + manual approve | Opaque embeddings |
| **Logs** | Every interaction → `~/.janus/log.jsonl`, FTS5-indexed | Best-effort |
| **Model-call path** | ~50 lines of `requests`, no SDK | litellm / openai-python / anthropic-python |
| **State format** | Markdown, JSON, JSONL, SQLite — all human-readable | Database-backed |

If you want a self-improving local agent that you can audit, edit, and
unwind by hand, Janus is for you. If you want a fully autonomous shell
co-pilot, look elsewhere.

---

## Install

Janus runs on **Windows, macOS, and Linux** with Python ≥ 3.10. The core
install is one dependency (`requests`) — everything else is opt-in.

### 1. Clone

```bash
git clone https://github.com/samalgotrader7-ops/janus.git
cd janus
```

### 2. Create a virtual environment

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

### 3. Install

#### Option A — pipx (recommended on Linux & macOS)

`pipx` puts `janus` on your PATH globally without needing to activate a
venv each shell. This is the durable install for servers and dev boxes.

```bash
sudo apt install -y pipx          # Ubuntu / Debian
# or: brew install pipx           # macOS
pipx ensurepath                    # adds ~/.local/bin to PATH (once)

pipx install -e ".[rich]"          # editable install + rich TUI
```

To later switch extras: `pipx uninstall janus-agent && pipx install -e ".[all]"`.

#### Option B — pip in a venv (works everywhere)

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

### 4. Smoke test

```bash
janus --version     # janus 0.13
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

[1] surgical refactor                                    risk: medium
    Move _dispatch + helpers into janus/dispatcher.py …

[2] split + add tests for new module                     risk: medium
    Same as [1] plus a tests/test_dispatcher.py covering …

[3] leave it alone, just document the boundary           risk: low
    Add a docstring section to cli_rich.py explaining …

pick [1-3], (r)efine, (s)kip, (q)uit:
```

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
| `/plan on\|off` | Toggle plan-tree mode (decompose into sub-tasks) |
| `/parallel on\|off` | Toggle parallel sub-agent execution |
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
user request
   │
   ▼
gateway  (cli / cli_rich / telegram / web / whatsapp / headless)
   │
   ▼
memory.prepend_for_prompt()  +  conversation.recent_context_block()
   │
   ▼
interpreter.interpret()  →  2–3 candidates
   │
   ▼
user picks  (or first candidate in headless / trusted-auto)
   │
   ▼
skills.match()  →  optional skill attach
   │
   ▼
planner.plan()  (when /plan on)  →  plan tree
   │
   ▼
orchestrator.run()  →  for each leaf:
   │   (parallel mode = subagent subprocess; serial = in-process)
   ▼
executor.execute()  →  tool-use loop:
   │     hooks.fire(PreToolUse) → maybe deny
   │     tools.call() → approver(action, details, capability=(...))
   │     hooks.fire(PostToolUse)
   ▼
final output
   │
   ▼
cost.record()  ·  conversation.add_turn()  ·  skill.record_run()
memory.propose_diff()  ·  logger.write()
```

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

## Safety invariants (P1–P10)

These are the rules every code change must respect. Full version in
[`docs/BUILD_GUIDE_FOR_CLAUDE_CODE.md`](docs/BUILD_GUIDE_FOR_CLAUDE_CODE.md) §3.

| | Invariant |
|---|---|
| **P1** | **Interpretation first** — no tool call ever fires before either the user picked an interpretation or a `trusted-auto` skill matched. |
| **P2** | **Capability-bounded execution** — every dangerous tool action is gated by capability tokens or explicit y/N. |
| **P3** | **Workspace as geometric boundary** — all fs/shell access resolves through `janus.security.resolve_within`. NOT a regex. |
| **P4** | **Manual skill promotion** — skills land as `quarantined`. Promotion is the user typing `/promote`. Never auto-promote. |
| **P5** | **Plain-text persistent state** — memory, skills, hooks, MCP config, conversations — all human-readable files. |
| **P6** | **No fat SDK in the model-call path** — the HTTP client is ~50 lines of `requests`. No litellm / openai-python / anthropic-python. |
| **P7** | **Bounded everything** — steps, depth, fanout, output bytes, fetch bytes, log retention, sub-agent count. |
| **P8** | **Errors are observations** — tool failures return strings the model reads; they do not raise into the executor loop. |
| **P9** | **Forward-compatible classes, not decorators** — each tool is a class. |
| **P10** | **Logged is owned** — every interaction, every tool call, every approval decision lands in `~/.janus/log.jsonl`. |

---

## Testing

```bash
pip install -e ".[test]"
pytest tests/ -q
```

The suite is fast (~12 s for 340+ tests) and uses no network or LLM
calls — `fake_llm` and `janus_home` fixtures isolate every run.

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
