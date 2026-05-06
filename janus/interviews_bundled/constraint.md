---
category: constraint
description: Hard limits — budget, time, environment, compliance, security.
version: 1
questions:
  budget:
    question: "Any budget constraints I should respect? (API costs, tooling, infrastructure)"
    mode: text
    importance: 0.85
    durability: 0.65
    recheck_days: 90
    placeholder: "e.g. <$50/mo on LLM APIs, no paid tools without asking"
  time:
    question: "Time constraints? (hard deadlines, work hours I shouldn't disturb)"
    mode: text
    importance: 0.8
    durability: 0.5
    recheck_days: 60
  environment:
    question: "Environment constraints? (specific OS, hardware, network restrictions)"
    mode: text
    importance: 0.7
    durability: 0.85
    recheck_days: 365
  compliance:
    question: "Any compliance / security rules I need to follow? (HIPAA, GDPR, SOC2, NDA, etc.)"
    mode: text
    importance: 0.9
    durability: 0.9
    recheck_days: 365
  hard_no:
    question: "Hard 'never do this' constraints? (one per line if multiple)"
    mode: text
    importance: 0.95
    durability: 0.95
    recheck_days: null
---

# constraint

Constraints are the rails Janus stays inside. Compliance and hard-no
constraints get permanent / yearly recheck — these are commitments to
external rules that don't move. Budget shifts more (90d). Hard-no rules
are NEVER auto-superseded by extraction (durability ≥ 0.9 → above the
0.7 protection threshold from v1.18).
