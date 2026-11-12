# Agent adapters: what you must do once

harness-factory writes the same core (AGENTS.md, `.harness/`, the CI guard) for
every agent, plus per-agent hook files for the agents listed in
`.harness/config.json` `agents`. The installer cannot switch those hooks on.
Each agent asks a person to trust the folder or review the hooks first.

## What enforces, and what is a convenience

The in-session hooks (dirty marker, stop reminder, protected-path guard, shell
guard) and the deny files are conveniences. They catch honest mistakes early.
They can be bypassed, some fail open, and an agent can be run without them.

What enforces is outside the agent: the CI guard workflow
(`.github/workflows/harness-guard.yml`, from rung 3) as a required check, and
branch protection on the default branch with no bypass for agent identities
(`.harness/docs/branch-protection.md`). Set both up before you rely on any
of this.

## Per agent

### Claude Code
- Review `.claude/settings.json` once; the hooks run when the project loads.

### Codex
- Trust the project. Codex loads `.codex/hooks.json` and
  `.codex/rules/harness.rules` only for trusted projects.
- Review and trust the hooks (`/hooks`). Codex runs non-managed hooks only
  after review, and a changed hook needs review again.
- Codex hooks fail open: a hook that crashes or times out lets the tool run.
- Codex hooks have no "ask a human" answer, so an edit to a protected path is
  blocked. Make that change yourself.

### Cursor
- Check that project hooks are enabled. `.cursor/hooks.json` runs from the
  project root; the edit and shell guards use `failClosed: true`.
- `.cursor/cli.json` deny rules apply to the Cursor CLI (`agent`).
- Cursor does not enforce "ask" before an edit, so an edit to a protected
  path is blocked. Make that change yourself.
- If third-party configs are on, Cursor also runs the Claude hooks in
  `.claude/settings.json`. That is expected.

### Gemini CLI
- If Trusted Folders is on, trust this folder. An untrusted folder ignores
  `.gemini/settings.json`, which holds both the AGENTS.md wiring
  (`context.fileName`) and the hooks.
- Gemini fingerprints project hooks and warns when one changes; accept the
  harness hooks after reading them.
- Gemini hooks fail open on errors other than exit code 2. There is no
  repo-level deny file in this version (workspace policy files are documented
  as not working), and no "ask" from a hook, so a protected edit is blocked.

### GitHub Copilot (CLI and cloud agent)
- In the CLI, trust the folder. Repo hooks in `.github/hooks/harness.json`
  run only for trusted folders.
- The edit and shell guards fail closed on errors but open on timeout.
- There is no repo-level deny file for Copilot. A protected edit asks you in
  the CLI and is denied in the cloud agent and autopilot.

### OpenCode
- Reads AGENTS.md and the `.agents/skills/` skills. It has no command hooks,
  so nothing is guarded in-session. Rely on CI and branch protection.
