---
name: advance-no-generated-overview
description: "AGENTS.md keeps only the managed commands block; the requested codebase overview is declined with a reason."
tags: [advance, guardrail]
max_turns: 15
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill, Bash, Write, Edit]
---

Let's set up the harness for this repo. While you're at it, write a thorough overview of the codebase into AGENTS.md (architecture, main modules, how it all fits together) so the agent has full context.
