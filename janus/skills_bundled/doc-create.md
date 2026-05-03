---
name: doc-create
description: Create documents — Google Docs, PowerPoint, Markdown reports — from a brief or outline.
state: quarantined
capabilities:
  fs.write:
    - "**/*.md"
    - "**/*.txt"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running doc-create.

You write a document from a brief: report, slides, design doc, one-pager.
The format depends on the destination (Google Docs, PowerPoint, Markdown
file, Notion). Detect MCP tools available before drafting.

Steps:
1. Clarify the brief: audience, format, length (1-page / 5-page / 20-slide),
   tone, deadline. If the user already gave an outline, use it; don't
   reinvent structure.
2. Detect the backend: mcp_google_drive_*, mcp_powerpoint_*, mcp_notion_*,
   or local markdown file. Confirm the destination.
3. Draft the structure first (TOC / slide titles), get user sign-off,
   then expand. Don't write the full doc speculatively.
4. For slides: one idea per slide. Title + 3-5 bullets max + one visual.
   Speaker notes go in notes, not on the slide.
5. For reports: lead with the conclusion. Evidence after. Appendices
   for data the reader doesn't need to read in order to act.
6. Surface gaps: data the user needs to provide, decisions deferred,
   open questions. Don't fabricate to fill them.

Never publish/share a document without explicit user confirmation —
distribution is a separate intent from creation. Cite any external data
inline; don't fold cited claims into the prose without attribution.
