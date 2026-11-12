# Rung 1: Legible

**Goal:** an agent can find its way around the repository and knows the real commands.

**Why first:** an agent that guesses the test command either skips testing or reports results from the wrong command. Everything later depends on these facts being right.

## Ask the user

- Which coding agents do you use here? (`agents`)
- Are these the right commands to set up, build, test, lint, type-check and run the project? Show the detected ones; correct them one by one. (`commands.*`)
- Which branch do changes merge into? (`default_branch`)

Leave a command empty when the project has none. A library has no `run` command.

## What gets installed

- `AGENTS.md`: a Commands table built from the config, and a "Things you cannot find in the code" section. Existing content is kept; the harness only manages its own marked blocks.
- Wiring so each configured agent reads `AGENTS.md` (for Claude Code, an `@AGENTS.md` line in `CLAUDE.md`).
- `.harness/README.md` and a few `.gitignore` lines.

## After install (the user's part)

- Fill in "Things you cannot find in the code": what must never change without a person, which commands are dangerous or cost money, which decisions would surprise a newcomer. Ask the user for the answers and write down what they say. Do not derive answers from reading the code, and do not add a description of the codebase.
- If the audit reports a tracked secret or a tracked local settings file, that is theirs to fix first: remove it from the repository and rotate the secret.

## Common blockers

| Blocker | What it means | Who fixes it |
|---|---|---|
| No test command found | The project has no tests, or runs them in a way the audit cannot see | The user names the command, or writes a first test |
| References do not resolve | The instructions file names a path or script that no longer exists | Correct or remove the stale line, with the user's agreement |
| Possible secret in tracked files | A key or token is committed | The user removes and rotates it |
