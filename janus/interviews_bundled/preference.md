---
category: preference
description: How you want me to communicate, work, and respond.
version: 1
questions:
  communication_style:
    question: "How should I communicate with you?"
    mode: choices
    choices:
      - "Terse + code-first"
      - "Explainer + walkthroughs"
      - "Formal + structured"
      - "Casual + friendly"
    importance: 0.9
    durability: 0.8
    recheck_days: 180
  ask_or_act:
    question: "When something is risky / destructive, should I always ask, or just do it (in auto mode)?"
    mode: choices
    choices:
      - "Always ask first"
      - "Just do it (in auto mode)"
      - "Depends — explain risks then ask"
    importance: 0.85
    durability: 0.8
    recheck_days: 180
  code_style:
    question: "When I write code for you, what should I include by default?"
    mode: choices
    choices:
      - "Just the diff"
      - "Code + brief comments"
      - "Code + tests"
      - "Code + tests + docstrings"
    importance: 0.8
    durability: 0.8
    recheck_days: 180
  response_length:
    question: "How detailed do you want my chat replies to be?"
    mode: choices
    choices:
      - "Very terse — one line"
      - "Concise — 2-3 sentences"
      - "Detailed when needed"
      - "Always thorough"
    importance: 0.75
    durability: 0.75
    recheck_days: 180
  emoji:
    question: "Use emojis in chat replies?"
    mode: choices
    choices:
      - "Never"
      - "Sparingly for tone"
      - "Freely"
    importance: 0.5
    durability: 0.8
    recheck_days: null
---

# preference

Preference questions shape HOW Janus interacts with you — independent
of WHAT you're working on. Most can be re-checked every 6 months
(preferences drift slowly). Emoji preference is permanent (people
don't usually flip on this).
