---
category: identity
description: Who you are, fundamentally — name, role, location, background.
version: 1
questions:
  name:
    question: "What should I call you? (just first name, no title)"
    mode: text
    importance: 0.95
    durability: 0.95
    recheck_days: null
  role:
    question: "What's your primary role / profession?"
    mode: text
    importance: 0.9
    durability: 0.85
    recheck_days: 365
    placeholder: "e.g. network engineer, ML researcher, founder"
  timezone:
    question: "Where are you based? (timezone helps me schedule things right)"
    mode: choices
    choices:
      - PT
      - MT
      - CT
      - ET
      - UTC
      - Europe
      - Asia
      - Other
    importance: 0.8
    durability: 0.9
    recheck_days: null
  years_experience:
    question: "How many years of experience in your primary field?"
    mode: text
    importance: 0.6
    durability: 0.7
    recheck_days: 365
  background:
    question: "Anything else about your background I should know? (other roles, fields, side-projects)"
    mode: text
    importance: 0.7
    durability: 0.75
    recheck_days: 365
---

# identity

Identity questions establish who Janus is talking to — drives tone,
language register, and recall priority. These cards get high durability
and rarely need re-asking. The `name` and `timezone` answers are
permanent (`recheck_days: null`); role + experience get re-checked
yearly because careers shift.
