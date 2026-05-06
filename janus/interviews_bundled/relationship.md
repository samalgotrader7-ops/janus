---
category: relationship
description: People in your work / life — team, clients, family, collaborators.
version: 1
questions:
  team:
    question: "Who else is on your team / project? (just first names + roles)"
    mode: text
    importance: 0.75
    durability: 0.7
    recheck_days: 180
  reporting:
    question: "Who do you report to (if applicable)? (or 'self-employed')"
    mode: text
    importance: 0.7
    durability: 0.85
    recheck_days: 365
  clients:
    question: "Any clients / customers I should be aware of?"
    mode: text
    importance: 0.7
    durability: 0.6
    recheck_days: 180
  external_collaborators:
    question: "External collaborators / consultants / partners?"
    mode: text
    importance: 0.65
    durability: 0.7
    recheck_days: 180
  personal:
    question: "Family / personal relationships that affect your schedule? (spouse, kids, caretaker duties — share what's relevant)"
    mode: text
    importance: 0.7
    durability: 0.85
    recheck_days: 365
---

# relationship

Relationship cards drive Janus's contextual awareness — knowing your
team helps with "send X to my team" / "my boss asked for Y". Personal
relationships affect scheduling (don't suggest 11pm Slack messages if
you have kids). All of these are SCOPE-SENSITIVE: by default they
land at the current origin (telegram chat / web session / cli /
project), NEVER global, per the v1.18 privacy invariant.
