---
category: decision
description: Choices you've made — what you committed to, what you ruled out.
version: 1
questions:
  tools_chosen:
    question: "Tools / services / frameworks you've COMMITTED to (so I don't suggest alternatives)?"
    mode: text
    importance: 0.8
    durability: 0.75
    recheck_days: 180
    placeholder: "e.g. Postgres for storage, FastAPI for HTTP, GitHub for code hosting"
  tools_avoided:
    question: "Tools / services you've explicitly RULED OUT?"
    mode: text
    importance: 0.75
    durability: 0.8
    recheck_days: 180
  recent_pivot:
    question: "Any major decisions you've made recently I should remember? (chose X over Y, switched to Z)"
    mode: text
    importance: 0.8
    durability: 0.7
    recheck_days: 90
  licensing:
    question: "Any licensing / IP constraints on what we build together? (open-source, proprietary, NDA)"
    mode: choices
    choices:
      - "Open-source by default"
      - "Proprietary by default"
      - "Mixed — depends on project"
      - "Under NDA — be careful"
    importance: 0.85
    durability: 0.85
    recheck_days: 365
  boundaries:
    question: "Anything you've decided is off-limits? (topics / tasks / scope I should refuse)"
    mode: text
    importance: 0.85
    durability: 0.85
    recheck_days: 365
---

# decision

Decisions are commitments — which tools you've picked, which you've
ruled out, what's off-limits. Long durability + slow recheck (most
decisions are made for the year). Recent pivots are an exception —
quarterly recheck because the "recent" window keeps moving.
