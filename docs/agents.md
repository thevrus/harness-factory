# Supported agents

The core of a harness is the same for every coding agent: `AGENTS.md`, the scripts in `.harness/`, and the CI guard workflow. Those work no matter which agent wrote a change.

What differs is the in-session wiring: how an agent is told to read `AGENTS.md`, how a script runs before an edit or before the agent finishes, and whether the repository can carry deny rules. Each supported agent has a small adapter for that. You choose which adapters are installed with the `agents` list in `.harness/config.json`:

```
python3 skills/harness-factory/scripts/hf_install.py config set --repo . agents '["claude", "codex"]'
```

The default is `["claude"]`. A repository gets no files for agents it does not list.

## What each adapter wires

Regenerate this table with `python3 skills/harness-factory/scripts/hf_audit.py --agents`.

| Agent id | Reads AGENTS.md | In-session edit guard | In-session stop gate | Repo-level deny rules | Fail mode on hook crash | User must do once |
|---|---|---|---|---|---|---|
| claude | only through `@AGENTS.md` in CLAUDE.md | PreToolUse hook in `.claude/settings.json` | Stop hook in `.claude/settings.json` | `.claude/settings.json` `permissions.deny` / `ask` | blocks when the hook sets `"onFailure": "block"` | nothing |
| codex | yes, natively | PreToolUse hook in `.codex/hooks.json` | Stop hook in `.codex/hooks.json` | `.codex/rules/harness.rules` (`prefix_rule(..., decision="forbidden")`, commands only) | fails open (the tool call continues) | trust the project, then review and trust the hooks in `/hooks` |
| copilot | yes, natively (cloud agent and CLI) | preToolUse hook in `.github/hooks/*.json` | agentStop hook in `.github/hooks/*.json` | none at repo level (cloud firewall is a repo setting; CLI uses `--deny-tool` flags) | preToolUse fails closed on a crash, open on a timeout | trust the folder in Copilot CLI (`--trust-folder` for one headless run) |
| cursor | yes, natively | preToolUse hook in `.cursor/hooks.json` | stop hook in `.cursor/hooks.json` (followup_message, loop_limit) | `.cursor/cli.json` `permissions.deny` (CLI only; editor allowlists are UI settings) | fails open unless the hook sets `"failClosed": true` | nothing for the CLI in a trusted workspace (`--trust` for headless runs) |
| gemini | only if `.gemini/settings.json` `context.fileName` lists it (or GEMINI.md imports `@./AGENTS.md`) | BeforeTool hook in `.gemini/settings.json` | AfterAgent hook in `.gemini/settings.json` | none reliable (workspace policies are documented as non-functional) | fails open (exits other than 0 and 2 are warnings) | trust the folder if Trusted Folders is on; accept changed project hooks |
| opencode | yes, natively (V2 reads AGENTS.md only) | none (no command hooks; TypeScript plugins only) | none documented | not audited in this version | n/a (no command hooks) | nothing |

## What to take from the table

- **In-session guards are a convenience in every agent.** Several fail open when a hook crashes, most need a person to trust the folder or the hooks first, and any agent can be started without them. The guards that hold are the CI guard as a required check and branch protection. Rung 3 requires both for that reason.
- **Protected paths behave differently.** Claude Code and Copilot CLI can ask the person before a protected file is edited. Codex, Cursor and Gemini CLI have no "ask" answer from a hook, so a protected edit is blocked and the person makes that change by hand. Acceptance scenarios are blocked in all of them.
- **OpenCode has no command hooks.** It reads `AGENTS.md` and the shared verify skill, and relies on CI for everything else.
- **After install, each agent needs one manual step** (last column). `.harness/docs/agents.md` in your repository repeats these steps for the agents you chose.

## Versions

Claude Code v2.1.295 or later is needed for the guard hooks to block when they crash or time out (the `onFailure` hook setting). On older versions the guards still run but fail open.

## Not in this version

- The unattended loops at rungs 4 and 5 (review, triage, implement, monitor) run on Claude only. The two-lane design does not depend on the vendor, and the write lane runs no model at all, but agent steps for other vendors' CI actions are not shipped yet.
- Sandbox settings are audited for Claude Code only.
- Other agents that read `AGENTS.md` and `.agents/skills/` (Amp, Warp and similar) get the core and the shared skill with no adapter, the same as OpenCode.

## How sure we are

Hook formats were taken from each vendor's published documentation in November 2026 and exercised with sample inputs, not with live sessions of every agent. Known unverified points: the path field names in Cursor and Copilot edit hooks (the adapters fall back to any path-like field and block if none is found), how Codex command rules interact with its sandbox, and whether Copilot accepts PascalCase event names. If a guard does not fire in your agent, the CI guard still catches the change; please open an issue with the hook input you saw.
