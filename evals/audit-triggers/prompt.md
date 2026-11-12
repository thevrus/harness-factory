---
name: audit-triggers
description: "Plain readiness question fires the skill, runs the audit script and states a rung."
tags: [audit, trigger]
max_turns: 15
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill, Bash, Write, Edit]
---

Is this repo ready for coding agents? Just give me a straight answer, I'm deciding whether to let Claude loose on it next week.
