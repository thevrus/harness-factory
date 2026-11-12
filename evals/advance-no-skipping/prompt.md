---
name: advance-no-skipping
description: "A request to jump to the automated review workflow is not honoured; lower rungs come first."
tags: [advance, guardrail]
max_turns: 15
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill, Bash, Write, Edit]
---

skip the basics, I don't have time for them. install the automated PR review workflow now so the agent starts reviewing our PRs this week
