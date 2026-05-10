# Tutorial 02 — Your First Skill

**Goal**: write a skill, see Janus auto-load it on the right
trigger, then promote it from quarantined to trusted. ~10 minutes.

**Prereq**: [Tutorial 01](01-hello-janus.md) — Janus installed
and configured.

---

## What a skill is

A markdown file in `~/.janus/skills/<name>/SKILL.md` with YAML
frontmatter and a body. The frontmatter declares **when** the
skill should auto-attach (`triggers`); the body is system-prompt
text Janus injects when it does.

Skills land **quarantined** by default — you `/promote` them
once you trust them.

## 1. Create a skill

Make a directory:

```bash
mkdir -p ~/.janus/skills/git-pr-review
```

Write `~/.janus/skills/git-pr-review/SKILL.md`:

```markdown
---
name: git-pr-review
description: Review the diff of the current branch against main.
state: quarantined
triggers:
  - "review the pr"
  - "review this branch"
  - "review my changes"
capabilities:
  shell: ["git diff --stat main..HEAD", "git log main..HEAD --oneline"]
---

When the user asks for a PR review, do the following:

1. Run `git diff --stat main..HEAD` to get the file-level summary.
2. Run `git log main..HEAD --oneline` to see the commit history.
3. Identify risky changes: large diffs to security-sensitive files,
   missing tests, refactors mixed with feature changes.
4. Suggest 1-3 specific improvements the author could make before
   asking for human review.

Be brief. The author wants signal, not a wall of text.
```

## 2. Trigger the skill

Run `janus` from inside a git repo with at least one commit ahead
of `main`. Then:

```
› review the pr
```

You should see:

```
🪄 skill: git-pr-review (quarantined — calls limited)
```

Janus loaded your skill, the body became part of the system prompt
for THIS turn, and the model ran the steps you described.

`quarantined` state means tool calls beyond what's listed in
`capabilities:` will prompt for permission. Once you've used the
skill a few times and trust it, promote:

```
› /promote git-pr-review
```

State becomes `trusted` — the listed `capabilities:` calls
auto-allow without per-call prompts.

## 3. Inspect what loaded

```
› /skills
```

Lists all skills with state + recent usage count. Pick one to view:

```
› /skills show git-pr-review
```

Prints the frontmatter + body Janus saw.

## 4. Auto-proposed skills

After a few sessions, Janus notices repeated patterns in your
turns and offers to draft skills for them. Watch for:

```
🪄 File 'src/components/Button.tsx' touched 9 times.
   /skills propose file-src-components-button-tsx to draft,
   /skills decline ... to silence.
```

Accepting drafts the skill (still quarantined — you review before
promoting). This is the **learning loop** that makes Janus
self-improving over time.

## What's next

→ [Tutorial 03 — Memory Loop](03-memory-loop.md)

Memory is the persistent narrative Janus builds about your work,
preferences, and project context. Tutorial 3 covers how memory
proposals appear, how to review them, and how to keep them sharp.
