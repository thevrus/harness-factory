---
name: harness-factory
description: Audit how ready a repository is for AI coding agents, then set up its agent harness and software factory one verified rung at a time (legible, verifiable, guarded, first loop, factory). Use for "is this repo ready for agents", an agent-readiness or harness audit, "set up a harness", "make this repo agent-ready", "build a software factory for this project", "why does the agent keep making this mistake", or a review of factory metrics. Works on new and existing repositories and with any coding agent; merges into existing AGENTS.md, CLAUDE.md and agent settings without overwriting them.
license: MIT
compatibility: Requires python3 (3.9+) and git. Uses gh for GitHub checks when available. Works in any agent that loads Agent Skills.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/hf_audit.py *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/hf_install.py plan *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/hf_install.py verify *)
metadata:
  version: "0.1.0"
---

# Harness Factory

A coding agent is only as reliable as what surrounds it: the instructions it reads, the checks that prove its work, and the guards that contain its mistakes. That surrounding is the harness. A software factory is a harness plus automation that runs agents without a person driving each step.

This skill builds both from the bottom up. A repository climbs five rungs, and a rung is installed only when everything below it is proven to work.

| Rung | Name | The repository gains |
|---|---|---|
| 1 | Legible | The agent can find its way and knows the real commands |
| 2 | Verifiable | "Done" can be proven: gates, a core-flow smoke run, evidence rules |
| 3 | Guarded | Mistakes are contained: protected paths, diff limits, merge guards |
| 4 | First loop | One unattended automation that can only comment or label |
| 5 | Factory | Triage, implement, monitor, metrics and a failure library |

Rungs 1 to 3 are the harness. Rungs 4 and 5 are the factory. Most repositories should stop after rung 3 until they have more agent work than a person can supervise.

## Where the scripts are

The work is done by two scripts in this skill's `scripts/` directory. Set `HF` to that directory before running anything:

```bash
HF="${CLAUDE_SKILL_DIR}/scripts"
```

If the line above still shows a `${...}` placeholder, your agent does not fill it in. Use the directory this `SKILL.md` was loaded from, followed by `/scripts`.

- `python3 "$HF/hf_audit.py"` decides which rung a repository is on. It is read-only.
- `python3 "$HF/hf_install.py"` writes harness files into a repository and verifies them.

## Pick a mode

Read the one reference file for what the user asked, then follow it.

| The user wants | Mode | Read |
|---|---|---|
| To know where the repository stands; a readiness check; nothing changed | audit | `references/audit.md` |
| To set up or improve the harness or factory; to fix audit blockers | advance | `references/advance.md` |
| To understand repeated agent mistakes, review metrics, or prune rules | tune | `references/tune.md` |

If it is unclear, run the audit first. It changes nothing and its output tells you which mode comes next.

## Rules that hold in every mode

- **The scripts decide; you assist.** The audit script scores the repository and the installer writes the files. Do not re-score by judgment and do not write harness files by hand.
- **No skipping rungs.** Automation on a repository that cannot prove its own work produces changes nobody can trust.
- **One rung per run.** Install it, prove it, stop.
- **Never weaken a check to pass it.** A stub that exits 0, an empty gate list or a guard that allows everything is worse than a failing check.
- **Do not generate a summary of the codebase.** The instructions file holds the commands and what a person knows that the code does not show. Generated overviews cost context on every turn and have been measured to lower task success.
- **Ask before running repository code.** The audit's `--run` option executes the project's own test and start commands.
- **Do not turn on unattended automation.** Workflows installed at rungs 4 and 5 stay off until a person sets the `AGENT_ENABLED` repository variable.
- **Do not commit or push** unless the user asks.

## Explaining it to the user

Users range from beginners to platform engineers. Lead with what the result means for them in plain words, then the detail. Every audit blocker carries a one-line reason and an exact fix; pass those on as written. For any single check, `python3 "$HF/hf_audit.py" --explain <ID>` prints what it looks for and why.

Say plainly when they do not need more. A solo project with a passing rung 3 has most of the benefit.
