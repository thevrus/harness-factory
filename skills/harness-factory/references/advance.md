# Advance a repository one rung

One invocation installs one rung. The ladder:

| Rung | Name | The repository gains |
|---|---|---|
| 1 | Legible | An agent can find its way and knows the real commands |
| 2 | Verifiable | "Done" can be proven: gates, a core-flow smoke run, evidence rules |
| 3 | Guarded | Mistakes are contained: protected paths, diff limits, merge guards |
| 4 | First loop | One unattended automation that can only comment or label |
| 5 | Factory | Triage, implement, monitor, metrics and a failure library |

Two rules hold throughout:

- **No skipping.** A rung is installed only when every required check below it passes. An unattended loop on a repository that cannot prove its own work produces unreviewable changes.
- **Scripts decide, you assist.** The audit script decides the rung. The installer writes the files. You gather the facts neither script can know and you never write harness files by hand.

## Procedure

### 1. Audit

```bash
python3 "$HF/hf_audit.py" --repo . --json "$TMPDIR/hf-audit.json"
```

Read `rung_achieved`, `next_rung`, `blockers` and `detected` from the JSON. If `next_rung` is null the repository is at rung 5; say so and stop.

If a blocker of an already-installed rung is failing (a regression), repair that first. Re-running the installer for that rung is safe; it is idempotent.

### 2. Gather the facts the scripts cannot know

Open `rung-N.md` (next to this file) for the next rung. It lists the questions for that rung and what the human must do after install.

Facts live in `.harness/config.json`. Create it once, prefilled from the audit:

```bash
python3 "$HF/hf_install.py" config init --repo . --from-audit "$TMPDIR/hf-audit.json"
```

Then set each fact the user confirms or supplies. The value is JSON, or plain text:

```bash
python3 "$HF/hf_install.py" config set --repo . commands.test "npm test"
python3 "$HF/hf_install.py" config set --repo . protected_paths '["migrations/**", "src/billing/**"]'
python3 "$HF/hf_install.py" config show --repo .
```

| Key | Meaning | Needed from |
|---|---|---|
| `agents` | Which coding agents this repository uses, for example `["claude", "codex"]` | Rung 1 |
| `commands.setup`, `build`, `test`, `lint`, `typecheck`, `run` | The real commands | Rung 1 (`test` is required at rung 2) |
| `default_branch` | The branch that changes merge into | Rung 1 |
| `core_flow.description`, `core_flow.smoke_command` | The one thing the app must be able to do, and a command that proves it | Rung 2 |
| `protected_paths`, `human_only_areas` | Files and areas that need a person's approval | Rung 3 |
| `max_diff_lines` | Largest change accepted without a person splitting it | Rung 3 |
| `maintainers` | GitHub logins allowed to start agent work | Rung 4 |
| `first_loop.review` or `first_loop.triage` | Which first automation to install; set one to `"yes"` | Rung 4 |
| `max_open_agent_prs`, `max_fix_attempts` | Limits on the implement loop | Rung 5 |
| `customized_scripts` | Guard scripts the user changed on purpose, so the audit accepts them | Only when needed |

- The prefilled commands are guesses from the project's files. Ask the user to confirm or correct each one.
- Ask only what the current rung needs. Do not interview for rung 4 while installing rung 1.
- An empty value means "not applicable or not known yet". Leave it empty; never invent a command, a path or a maintainer.
- Always change the config through `config set`. From rung 3 on, the config is a protected file and direct edits are blocked. The command asks the user for permission, and that prompt is their approval.

### 3. Plan, show, apply

```bash
python3 "$HF/hf_install.py" plan --repo . --rung N
```

Show the user the plan: files created, files merged into, conflicts. Apply only after they agree:

```bash
python3 "$HF/hf_install.py" apply --repo . --rung N
```

The installer refuses to install a rung until the rung below is proven. A failing required check on a lower rung stops it. So does a check it cannot measure on its own (the gates and smoke run, branch protection), unless the last recorded audit from step 6 shows the lower rung achieved. That is why every rung ends with `hf_audit.py --run --append-ledger`. Fix what it names; do not look for a way around it.

On a conflict the installer stops before writing anything. Show the conflicting files and let the user choose:

- `--on-conflict sidecar` writes the new version beside the existing file as `<file>.harness-new` for a manual merge.
- `--on-conflict replace` backs the existing file up to `<file>.harness-bak` and overwrites it.

Never choose `replace` on the user's behalf.

### 4. Verify what is in effect

```bash
python3 "$HF/hf_install.py" verify --repo . --rung N
```

This checks the merged result, not the copy: managed blocks present, settings fragments merged, every registered hook pointing at an executable file. If it fails, report the miss and stop.

### 5. Finish the human part

Walk the user through the "After install" list in `rung-N.md` (next to this file). Some steps are theirs alone: writing down what cannot be found in the code, writing the smoke command, turning on branch protection, adding repository secrets. Ask; do not fill these in from guesses.

### 6. Prove it and record it

Re-run the audit. With the user's agreement, use `--run` so the checks that need execution are measured, and `--append-ledger` so the result is recorded:

```bash
python3 "$HF/hf_audit.py" --repo . --run --append-ledger
```

`--run` executes the repository's own gate and smoke scripts. Ask before the first use in a session.

Report the rung now achieved, or the blockers that remain and who has to clear them. Then stop. The next rung is a separate invocation, preferably after the repository has been used at this rung for a while.

## What you must not do

- **Do not generate a repository overview.** `AGENTS.md` gets the commands and the facts a human supplies. Generated summaries of the code cost tokens on every turn and have been measured to lower task success.
- **Do not weaken a check to make a rung pass.** A stub that exits 0, an empty gate list, or a guard set to allow everything is worse than a failing check.
- **Do not edit files the installer manages.** Change the config with `config set` and re-run the installer.
- **Do not turn on unattended automation yourself.** Rungs 4 and 5 install workflows that stay off until a person sets the `AGENT_ENABLED` repository variable.
- **Do not commit or push** unless the user asks.

## New repositories

An empty repository starts at rung 0 like any other. Rung 1 needs a manifest and a test command, so the first step is the user's own project scaffold. Once a build and a test command exist, proceed as above. Installing the harness early is cheap; rungs 4 and 5 are not worth installing until there is real work flowing through the repository.
