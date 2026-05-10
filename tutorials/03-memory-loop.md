# Tutorial 03 — Memory Loop

**Goal**: see Janus propose memory updates after a turn, review
them, accept or reject, then watch the next turn use what you
saved. ~10 minutes.

**Prereq**: [Tutorial 02](02-your-first-skill.md) — basic skill
flow.

---

## What memory is

Plain markdown files in `~/.janus/memory/`:

- `MEMORY.md` — the index. Listed verbatim in every system prompt.
- `<topic>.md` — individual memory cards. Loaded on-demand when
  their description matches the current turn.

Memory categories (from the index file's section headers):

- **User** — who you are, how you work, what you know.
- **Feedback** — guidance you've given (corrections AND
  validations).
- **Project** — what's in flight, why, by when.
- **Reference** — pointers to where info lives (Linear / GitHub /
  Slack / etc.).

You can edit any of this in `$EDITOR`. Janus re-reads on the
next turn.

## 1. Tell Janus something memorable

Run a turn that contains a fact worth remembering:

```
› I work on the trading-system-v2 project. The repo is at
  ~/code/trading-v2. I'm on the data-pipeline team.
```

Janus replies normally. After the turn finishes, you'll see:

```
🧠 memory: 2 update(s) proposed — review with /memory review
```

## 2. Review the proposals

```
› /memory review
```

Output:

```
Pending memory updates (2):

1. ADD project_trading_system_v2.md
   ---
   name: Trading system v2
   description: Sam's main project. Repo at ~/code/trading-v2.
                Sam is on the data-pipeline team.
   type: project
   ---
   ...

2. UPDATE MEMORY.md
   + - [Trading system v2](project_trading_system_v2.md) — main project; data-pipeline team
```

Each proposal is a diff Janus is asking permission to apply.

## 3. Approve, edit, or reject

```
› /memory accept 1
› /memory accept 2
```

Or reject:

```
› /memory reject 1
```

Or apply with edits:

```
› /memory edit 1
```

Opens the diff in `$EDITOR`. Save your changes and Janus uses your
edited version.

## 4. See it in action

Start a fresh conversation:

```
› /clear
```

Then ask something context-dependent:

```
› what's the path to my main project?
```

Janus knows because `MEMORY.md` is in every system prompt and
`project_trading_system_v2.md` matched the question's description
field. No need to repeat yourself across sessions.

## 5. Memory hygiene

Memories age. Some get wrong (you renamed the repo, switched
teams, etc.). Three commands keep things fresh:

```
/memory list                  show all memories with last-update times
/memory rm <file>             delete a stale memory
/memory consolidate           ask the model to summarize + dedupe
                              (asks before applying — same review flow)
```

`/memory consolidate` is also available as a daily cron (set
`JANUS_MEMORY_CONSOLIDATE_HOURS=24` in your env and the daemon
runs it).

## What's next

→ [Tutorial 04 — Connect MCP](04-connect-mcp.md)

MCP (Model Context Protocol) is the Anthropic-led standard for
plugging external tools into LLM agents. Tutorial 4 shows how to
connect a stdio MCP server (filesystem, git, sqlite, etc.) and
expose its tools to Janus.
