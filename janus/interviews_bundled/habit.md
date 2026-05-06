---
category: habit
description: Recurring routines, work cadence, daily tools.
version: 1
questions:
  work_hours:
    question: "What time of day do you usually work?"
    mode: choices
    choices:
      - "Early morning (5-9 AM)"
      - "Morning (9-12)"
      - "Afternoon (12-5)"
      - "Evening (5-9 PM)"
      - "Late night (9 PM-2 AM)"
      - "Whenever — no fixed schedule"
    importance: 0.7
    durability: 0.7
    recheck_days: 90
  work_cadence:
    question: "What's your typical work cadence?"
    mode: choices
    choices:
      - "Daily focused blocks"
      - "Sporadic / project-based bursts"
      - "Weekday 9-5"
      - "Always on"
      - "Weekends + evenings only"
    importance: 0.65
    durability: 0.65
    recheck_days: 90
  tools_daily:
    question: "What tools / apps do you use most days? (editor, terminal, IDE, etc.)"
    mode: text
    importance: 0.7
    durability: 0.75
    recheck_days: 180
  routines:
    question: "Any recurring rituals I should know about? (daily standup, Friday review, weekly planning, etc.)"
    mode: text
    importance: 0.65
    durability: 0.7
    recheck_days: 90
  check_in_cadence:
    question: "How often should I check in (when running scheduled agents)?"
    mode: choices
    choices:
      - "Every few hours"
      - "Daily"
      - "Weekly"
      - "Only when something happens"
    importance: 0.75
    durability: 0.8
    recheck_days: 180
---

# habit

Habits are notoriously hard to enumerate cold — most people can't list
their habits on demand. Janus also infers habits from observed
conversation patterns (Phase 7 inferred suggestions). The cold-start
questions here are the EASY ones (work hours, cadence, daily tools);
the harder "what do you do every Tuesday at 3pm" stuff is left to
inference.
