# Rung 2: Verifiable

**Goal:** one command says pass or fail, and the agent may not finish until it passes.

**Why here:** without proof, "done" is the agent's opinion. This rung is where most of the quality gain comes from.

## Ask the user

- What is the one thing this software must be able to do? (`core_flow.description`)
- What command proves it, start to finish, in under a couple of minutes? (`core_flow.smoke_command`) Examples: start the server and request the health endpoint; run the command-line tool on a sample input and compare the output; run one end-to-end test.
- Confirm the test command is the one to gate on. (`commands.test`)

If they cannot name a smoke command yet, leave it empty. The installer ships a placeholder that fails on purpose, and the rung stays open until it is replaced. That is the honest state.

## What gets installed

- `.harness/bin/gates.sh` and `.harness/gates.conf`: runs each gate and ends with one line, `HARNESS_GATES: PASS`, `FAIL` or `MISCONFIGURED`. A gate that cannot run counts as misconfigured, never as passed.
- `.harness/bin/smoke.sh`: runs the core-flow command and ends with `HARNESS_SMOKE: PASS` or `FAIL`.
- `.harness/bin/prove-test.sh`: shows that a new test fails without the change it is meant to cover.
- `.harness/docs/evidence.md`: what counts as evidence and what does not.
- A hook that stops the agent from finishing while code has changed since the last passing gate run (for agents that support it).
- An Evidence section in the pull request template.

## After install (the user's part)

- Run the audit with `--run` once, with their agreement, to prove the gates and the smoke run pass on the current code.
- If gates fail on existing code, that is a finding about the project. Do not loosen the gate. Either fix the failure or, with the user's decision, mark that gate `optional` in `.harness/gates.conf` and say why in a comment.

## Common blockers

| Blocker | What it means | Who fixes it |
|---|---|---|
| No tests exist | Nothing can be proven yet | The user decides what the first tests cover; you can help write them when asked |
| Tests need services that are not running | The suite depends on a database or an external system | Add a setup command, or split out the tests that run without it |
| Tests are slow or flaky | The agent will wait a long time or get false failures | Gate on a fast, reliable subset; keep the full suite in continuous integration |
| Smoke is still the placeholder | No core-flow command has been written | The user supplies one |
