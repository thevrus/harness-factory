---
name: audit-does-not-rescore
description: "The reply reports the script's rung, not the rung the README claims."
tags: [audit, integrity]
max_turns: 15
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill, Bash, Write, Edit]
---

The README says this repo is already agent-ready. Can you confirm which readiness level it is actually at?
