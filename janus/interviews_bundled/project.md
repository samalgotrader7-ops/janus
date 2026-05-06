---
category: project
description: Active and recent work — code, products, side projects.
version: 1
questions:
  current_active:
    question: "What's the project you're most actively working on? (one sentence)"
    mode: text
    importance: 0.9
    durability: 0.5
    recheck_days: 60
  status:
    question: "What stage is it at?"
    mode: choices
    choices:
      - "Early exploration"
      - "Building"
      - "Shipping"
      - "Maintenance"
      - "Sunsetting"
    importance: 0.75
    durability: 0.5
    recheck_days: 60
  tech_stack:
    question: "What's the tech stack? (languages / frameworks / key tools)"
    mode: text
    importance: 0.8
    durability: 0.7
    recheck_days: 180
  repo_location:
    question: "Where does the code live? (path or URL — optional)"
    mode: text
    importance: 0.6
    durability: 0.85
    recheck_days: null
  collaborators:
    question: "Anyone else working on it with you? (or 'just me')"
    mode: text
    importance: 0.65
    durability: 0.6
    recheck_days: 90
---

# project

Project state changes fast — a 60-day recheck on the active project +
status keeps Janus from referring to "the project you're shipping" six
months after you sunset it. Repo location is permanent (where the code
lives doesn't drift); tech stack lasts longer (180d).
