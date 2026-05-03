---
name: email-toolkit
description: Send, search, and triage email — terminal (himalaya) and Gmail (via MCP) supported.
state: quarantined
capabilities:
  shell.exec:
    - "himalaya *"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running email-toolkit.

You send, search, and triage email through whichever backend is available:
- `himalaya` CLI for terminal-native IMAP/SMTP, if installed
- MCP Gmail tools (mcp_gmail_*), if a Gmail MCP server is connected

Steps:
1. Detect the backend. `which himalaya` or check for mcp_gmail_* tools in
   the registry. If neither, say so and stop.
2. For SEARCH: free-text query, sender filter, date range. Return a
   structured list (sender, subject, date, snippet) — never the full body
   unless the user asks.
3. For TRIAGE: bucket inbox into respond-now / respond-later / archive /
   filter-rule-candidate. Propose a filter rule for high-volume senders.
4. For SEND: confirm recipients, subject, and body BEFORE sending. Never
   send to a list address (>5 recipients) without explicit confirmation.
   Never send during user-marked quiet hours unless flagged urgent.
5. For DRAFT: write the draft, save to drafts folder, return the draft id —
   do not send.

Privacy rules: never paste the body of an email into a tool result that
will be logged unless the user asks. Mask reply-quoted emails to one line.
For attachments, read metadata only unless the user explicitly asks for
content. The plain-text agent log is auditable — assume someone other
than the sender will read it later.
