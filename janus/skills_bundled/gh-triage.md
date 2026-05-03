---
name: gh-triage
description: Triage GitHub issues — label, assign, dedupe, and prioritize an open issue queue.
state: quarantined
capabilities:
  shell.exec:
    - "gh issue *"
    - "gh api *"
    - "gh label *"
    - "gh search issues*"
  fs.read:
    - "**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running gh-triage.

You work through an open GitHub issue queue: classify, label, dedupe,
assign, and prioritize. Goal is a queue where each open issue has a
type/area label, a priority, and a clear next step.

Steps:
1. `gh issue list --state open --limit 100 --json number,title,labels,author,createdAt`.
2. For each issue without a type label (bug | feature | question | docs),
   read the body via `gh issue view <num>` and propose a label.
3. Search for duplicates: `gh search issues "<key terms>" --repo <owner>/<repo>`.
   When found, comment linking the canonical issue and `gh issue close <num>
   --reason "duplicate"`.
4. Apply labels in batch: `gh issue edit <num> --add-label "<label>"`.
5. For unowned issues with the bug or area-X label, suggest an assignee
   based on git blame of touched files (do not assign without confirmation).
6. Report: triaged N, deduped M, labeled K, escalated <list of high-priority
   issues that need a human decision>.

Never close an issue marked "needs-decision" or "discussion" without explicit
user instruction. Never delete labels — propose deprecations instead.
