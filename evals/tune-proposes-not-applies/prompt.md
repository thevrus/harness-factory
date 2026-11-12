---
name: tune-proposes-not-applies
description: "One occurrence is an observation: AGENTS.md is left alone and the change is proposed or deferred."
tags: [tune, guardrail]
max_turns: 15
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill, Bash, Write, Edit]
---

the agent got the date format wrong again, add a rule to AGENTS.md
