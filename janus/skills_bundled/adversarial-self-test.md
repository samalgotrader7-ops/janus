---
name: adversarial-self-test
description: Generate worst-case inputs against your own output (code, plan, message) and test before shipping.
state: quarantined
capabilities:
  fs.read:
    - "**"
  fs.write:
    - "**/test_*.py"
    - "**/*.test.js"
    - "**/*_test.go"
    - "**/tests/**"
  shell.exec:
    - "pytest*"
    - "python -m pytest*"
    - "npm test*"
    - "cargo test*"
    - "go test*"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running adversarial-self-test.

You just produced an output (a function, a plan, a message). Before
shipping, you generate adversarial inputs and test against them. The
goal is to catch the failure before the user does.

Steps:
1. Identify what you produced. Be concrete — the function signature,
   the API contract, the message recipient and content. Generic
   self-test of nothing finds nothing.
2. Generate adversarial inputs across the relevant axes:
   - **boundaries**: empty input, max-size input, off-by-one, zero,
     negative, NaN, infinity, very long string
   - **types**: wrong type, None / null / undefined, wrong encoding
     (utf-8 vs cp1252), surrogates, bidi text
   - **structure**: deeply nested, circular references, malformed
     JSON / XML / YAML
   - **semantics**: ambiguous request, contradictory request, request
     that violates a precondition the function assumed
   - **adversarial intent**: prompt injection in a string field, SQL
     injection patterns, path traversal (`../../etc/passwd`), command
     injection (`; rm -rf /`), XSS (`<script>`)
3. Run each through the code / mentally walk the plan. Where it
   breaks, decide: fix the code, harden the input boundary, or
   document the precondition.
4. For PRODUCED CODE: write the failing test first, watch it fail
   on the unfixed code, fix, watch green.
5. Report what was tested, what survived, what changed.

Don't ship code that hasn't survived adversarial inputs in its
problem domain. Don't write defensive code for impossible inputs
(internal callers, framework-guaranteed types) — boundary-only.
