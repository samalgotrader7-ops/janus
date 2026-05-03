---
name: calendar-plan
description: Read calendar, plan meetings, detect conflicts, draft agendas — Google Calendar via MCP.
state: quarantined
capabilities:
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running calendar-plan.

You work with the user's calendar through whatever MCP server is
connected (Google Calendar, Outlook, etc.). Identify available tools at
the start of the turn — usually mcp_calendar_* or mcp_google_calendar_*.

Steps:
1. Detect the backend: list MCP tools in the registry; if no calendar
   server is connected, say so and stop.
2. For READ: list events in a window (today, this week, custom range).
   Return a structured list (start, end, title, attendees count).
3. For SCHEDULE: confirm attendees, duration, and time-zone preferences
   BEFORE creating. Check conflicts in the proposed slot for the user
   and (if available) for the attendees.
4. For RESCHEDULE: notify attendees in the same call (don't create the
   new event before canceling the old one).
5. For AGENDA: read prior meetings with the same attendees, last shared
   doc, and any open action items. Draft a 3-5 bullet agenda — concise,
   not a wall of text.

Never create or modify events without explicit user confirmation —
calendar mutations are visible to other people. Never include sensitive
notes in event descriptions. Time zones are always explicit (never
"3pm" without a TZ).
