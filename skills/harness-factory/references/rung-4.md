# Rung 4: First loop

**Goal:** one automation runs without a person starting it, and it can only comment or label.

**Why here, and whether at all:** this is the first factory rung. It is worth installing when the repository has a steady flow of pull requests or issues. For a solo or quiet project, stop at rung 3 and say so.

## Ask the user

- Which loop first? (`first_loop.review` or `first_loop.triage`, set one to `"yes"`)
  - **Review:** an agent reads each pull request from a trusted contributor and posts one comment with findings and a merge-risk rating. Start here if the team already reviews pull requests.
  - **Triage:** an agent reads new issues and applies one label from a fixed list. Start here if issues pile up unsorted.
- Which GitHub logins may start agent work and whose review comments count? (`maintainers`)

## What gets installed

- One workflow, split into lanes: the job that reads issue or pull request text has no permission to write, and the job that writes never runs the model. Text from an outsider cannot both steer the agent and act on the repository.
- The prompt, the output schema and the validator the write lane uses.
- `.harness/maintainers.txt` and, for triage, the label allowlist.
- `.harness/docs/first-loop.md`: setup, operation and rollback.

Nothing runs until the user turns it on.

## After install (the user's part)

Each of these is done in the repository settings; `.harness/docs/first-loop.md` has the commands:

1. Add the model credential as a repository secret, preferably one owned by the organization with a spending limit.
2. Create the repository variable `AGENT_ENABLED` and set it to `true`. This is the off switch: setting it to `false` stops every agent workflow.
3. Create the labels, including `agent:triage-failed` (triage only), which marks an issue whose agent result was rejected so the hourly sweep moves past it. It must not be added to `.harness/triage-labels.txt`.
4. Test the off switch once: set it to `false`, trigger the workflow, confirm nothing ran.

Suggest they watch the first ten or so runs and read the output before trusting it. An automated reviewer adds cost; whether it earns that cost is something to measure, not assume.

## Common blockers

| Blocker | What it means | Who fixes it |
|---|---|---|
| Maintainers list empty | Nobody is allowed to start agent work | The user names the logins |
| Workflow lint fails | The installed workflow was edited into an unsafe shape | Restore it by re-running the installer, or fix the named line |
