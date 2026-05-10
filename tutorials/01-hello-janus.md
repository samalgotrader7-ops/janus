# Tutorial 01 — Hello, Janus

**Goal**: install Janus, configure a model, and have your first
chat turn. ~5 minutes.

---

## 1. Install

The fastest path is the one-line installer:

```bash
curl -sSL https://raw.githubusercontent.com/samalgotrader7-ops/janus/main/scripts/install.sh | sh
```

It detects your platform (Linux / macOS), installs `pipx` if
missing, and pulls Janus from PyPI.

Alternatively:

```bash
pipx install 'janus-agent[all]'
```

Verify:

```bash
janus --version    # prints something like 1.34.x
```

## 2. Configure a model

Janus needs three env vars: an API key, an API base URL, and a
model id. The fastest way is the onboarding wizard:

```bash
janus onboard
```

It walks you through provider selection (OpenRouter, OpenAI,
Anthropic, or local Ollama), key entry, and model choice. If you
already have `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or
`OPENROUTER_API_KEY` exported, the wizard auto-detects them.

Or set directly:

```bash
export JANUS_API_KEY="sk-..."
export JANUS_API_BASE="https://openrouter.ai/api/v1"
export JANUS_MODEL="anthropic/claude-haiku-4-5"
```

Persist these to `~/.janus/.env` so they survive shell restarts.

## 3. First turn

```bash
janus
```

You'll see the Janus banner, then a prompt:

```
       ╱─►
   ●──┼─►   janus  v1.34.x
       ╲─►   the agent that learns from you · plain-text everything

   ›
```

Type a request:

```
› what files are in this directory?
```

Janus calls `fs_grep` (or `shell ls`) and reports back. Notice:

- The agent runs tools INLINE — no separate "agent mode" toggle.
- A permission prompt appears for write/exec actions in default
  mode (you can change this with `/mode acceptEdits` or
  `/mode plan`).
- The conversation persists — `/resume` reopens the picker on
  next launch.

## 4. Useful slash commands

```
/help                show the full command list
/mode <name>         change permission mode
/cost                token usage + budget
/skills              list skills (next tutorial)
/memory              browse memory diffs (tutorial 3)
/mcp catalog         MCP servers (tutorial 4)
/clear               start a fresh conversation
```

## What's next

→ [Tutorial 02 — Your First Skill](02-your-first-skill.md)

A skill is a markdown file Janus auto-loads when its trigger
matches. Skills carry domain knowledge, capability tokens, and
optional tools — the building blocks of an extensible agent.
