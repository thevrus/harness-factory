# Rung 3: Guarded

**Goal:** mistakes stay small and visible. Sensitive files need a person, oversized changes are flagged, and the agent cannot merge its own work.

**Why here:** from this point the repository is safe to use with less supervision. Nothing unattended should be installed before this rung holds.

## Ask the user

- Which files or folders should never change without a person approving? Typical answers: database migrations, authentication, billing, infrastructure, deployment. (`protected_paths`)
- Which areas of the product should a person always decide? (`human_only_areas`, as paths)
- Is 400 changed lines a reasonable upper limit for one change? (`max_diff_lines`)

The harness's own files, agent settings, instructions files and workflow files are protected by default.

## What gets installed

- `.harness/protected-paths.txt`: the list, with the user's entries synced in from the config.
- `.harness/bin/diff-guard.sh`: fails a change that touches a protected path or is too large.
- In-session guards for agents that support hooks: editing a protected file asks the person first, and merging or pushing to the default branch is blocked.
- `.harness/acceptance/`: a place for acceptance scenarios written by people. The agent cannot edit them, because an agent that can edit the tests it is judged on will.
- `.github/workflows/harness-guard.yml`: runs the gates, the diff guard and a workflow safety linter on every pull request.
- `.harness/docs/branch-protection.md`: the exact settings to turn on at GitHub.

## After install (the user's part)

This rung cannot complete from files alone. The user must, in the repository settings on GitHub:

1. Protect the default branch: require a pull request, at least one approval, and the `harness-guard` checks.
2. Make sure automation cannot approve pull requests.
3. Optionally add a `CODEOWNERS` file so protected areas need their owner's review.

`.harness/docs/branch-protection.md` lists each setting and a command to confirm it. Then re-run the audit without `--offline`; the branch protection check reads the live settings.

In-session guards are a convenience. An agent determined to get around a local script can. The settings on GitHub are what hold, which is why this rung requires them.

## Common blockers

| Blocker | What it means | Who fixes it |
|---|---|---|
| Branch protection unknown | `gh` is missing or not signed in, or the audit ran offline | The user signs in to `gh`, or checks the settings by hand |
| Branch protection off | Anyone, including an agent, can push to the default branch | The user, in repository settings (needs admin rights) |
| Workflow lint fails | An existing workflow passes untrusted text into a shell or an agent, or uses an unpinned action | Fix the workflow; the finding names the file and line |
