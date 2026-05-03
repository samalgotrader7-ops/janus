---
name: qa-exploratory
description: Exploratory QA testing of web apps via the browser tool — find bugs, write reproducers.
state: quarantined
capabilities:
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running qa-exploratory.

You exercise a web app exploratorily: navigate, click, type, observe,
look for bugs. Output is a structured bug report, not a pass/fail.

Steps:
1. Confirm the URL and the test scope (the whole app / one feature /
   a specific user flow). Don't QA across out-of-scope features.
2. Plan a session: 5-10 paths to exercise. Mix happy paths and
   adversarial inputs (empty form, very long input, special chars,
   unicode, multi-byte, paste from rich text, network throttle).
3. Execute via the browser tools (browser_navigate, browser_snapshot,
   browser_click, browser_type, browser_text). After each action,
   snapshot and OBSERVE — don't assume the app responded the way you
   expected.
4. Catalog issues as you find them. For each:
   - severity (critical / major / minor / cosmetic)
   - reproducer (exact steps from a known starting state)
   - actual vs expected
   - screenshot (if visual)
5. Report at the end: a numbered bug list ordered by severity, plus
   a notes section on overall UX impressions.

Never submit forms with real user data (email signups, payments, etc.)
unless the user explicitly says it's a sandboxed environment. Don't
file bugs into a tracker from this skill — output is the user's;
they decide where it goes.
