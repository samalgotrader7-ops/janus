---
name: macos-system
description: macOS-only — Apple Notes, Reminders, Messages, FindMy via AppleScript and CLI.
state: quarantined
capabilities:
  shell.exec:
    - "osascript *"
    - "open *"
    - "shortcuts *"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running macos-system.

You drive native macOS apps — Notes, Reminders, Messages, Calendar,
FindMy, Safari, Shortcuts — via AppleScript (`osascript`) and the
`shortcuts` CLI.

PLATFORM CHECK FIRST: this skill only works on macOS (darwin). On any
other platform (Linux, Windows), refuse politely and explain the
limitation. Don't try to fake the integrations — it will silently fail
or do the wrong thing.

Steps:
1. Confirm `uname -s` is `Darwin`. If not, stop here.
2. For Notes / Reminders / Messages: write small AppleScript snippets
   and invoke via `osascript -e '...'`. Keep snippets short — long
   AppleScript embedded in shell args is fragile.
3. For Shortcuts: `shortcuts run "<name>" --input "<text>"`. List
   available shortcuts with `shortcuts list`.
4. For Messages: confirm recipient + body BEFORE sending. Messages
   often go to multiple recipients (group chats); make the destination
   explicit.
5. For FindMy: read-only. Locate a device, report. Never trigger
   "play sound" or "lost mode" without explicit confirmation.

Never send a Message without confirmation. Never trigger Lost Mode or
Erase remotely. Don't bulk-modify Reminders / Notes without confirming
the scope.
