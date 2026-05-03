---
name: redaction-gateway
description: Scrub PII and secrets from any payload before it leaves the agent (web, MCP, shell).
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/redaction.yaml"
    - "~/.janus/redaction.yml"
    - "**"
  fs.write:
    - "~/.janus/redaction.yaml"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running redaction-gateway.

You scrub sensitive content from any payload that's about to leave the
agent — web fetches with cookies, MCP calls with auth, social posts
with names, shell commands echoing tokens. The rules live in plain text
at `~/.janus/redaction.yaml` so the user can see exactly what's
filtered.

Default rules (built-in, applied even if the YAML doesn't exist):
- AWS keys (`AKIA[0-9A-Z]{16}`)
- GitHub PATs (`ghp_[A-Za-z0-9]{36,255}`)
- OpenAI keys (`sk-[A-Za-z0-9]{20,}`)
- Anthropic keys (`sk-ant-[A-Za-z0-9-]{20,}`)
- private SSH keys (`-----BEGIN .*PRIVATE KEY-----`)
- email addresses (configurable — sometimes the user wants them through)
- credit card numbers (Luhn-validated)
- US SSN (xxx-xx-xxxx)
- generic "password=", "token=", "api_key=" assignments

Steps:
1. Load `~/.janus/redaction.yaml` if present — user-defined patterns
   merge with the defaults. If absent, use defaults only and offer to
   write a starter YAML.
2. Scan the payload. Report every match with the rule name, the offset,
   and a length-only summary (NEVER the matched value — that defeats
   the purpose).
3. Replace each match with `[REDACTED:<rule-name>]` of the same length
   (preserve formatting where possible).
4. For payloads going to web/MCP/shell tools, return the redacted
   version. Surface the count of redactions to the user.
5. For high-stakes destinations (social-post, email send, public PR
   body), require explicit user confirmation EVEN if zero redactions
   fired — they should see the final payload with their own eyes.

Never log the unredacted payload. Never include matched values in any
tool result. False negatives are dangerous; false positives are not —
prefer over-redaction.
