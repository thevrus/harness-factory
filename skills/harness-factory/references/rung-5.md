# Rung 5: Factory

**Goal:** agents take approved issues, write the change, prove it, and open a draft pull request. Results are measured, and repeated mistakes become new checks.

**Whether at all:** install this only when the first loop has run for a while and its output is trusted, and when there are more small, well-specified issues than people have time for. Most repositories do not need this rung.

## Ask the user

- How many agent pull requests may be open at once? (`max_open_agent_prs`, default 3). Review is the bottleneck; a low number keeps the queue honest.
- How many times may the agent try to fix failing gates before it stops and reports? (`max_fix_attempts`, default 2)

## What gets installed

- An implement workflow. It starts only when a maintainer applies the `agent:ready` label or dispatches it by hand. It claims the issue by pushing a fixed branch name, works on that branch, runs the gates and the diff guard, and opens a draft pull request. It never merges.
- A monitor workflow that runs on a schedule, computes metrics, and files at most one issue per run when agent changes are reverted or the default branch breaks.
- `.harness/bin/metrics.py`: automation rate, first-pass acceptance, human touches, cycle time, revert rate and cost where available, each with its denominator and a comparison with human pull requests.
- A failure library (`.harness/failures/`), a taxonomy of failure signatures, and a rules inbox that collects maintainer corrections for review.
- `.harness/docs/factory.md`: how the pieces fit, how to pause everything, how to promote a correction into a rule.

## After install (the user's part)

1. Read `.harness/docs/factory.md` end to end. They are now operating this.
2. Fill in the two `TOOLCHAIN SETUP` blocks in `.github/workflows/agent-implement.yml` (jobs `implement` and `verify-gates`): the language setup actions, pinned to full SHAs, and `commands.setup` in `.harness/config.json` for installing dependencies. The agent cannot install anything itself, so without this the gates fail on every run and every draft is titled `[gates failing]`.
3. Decide where the holdout scenarios live. They must be outside what the agent can read, or the agent will write to the test.
4. Start with issues that are small, specified and reversible. Label only those `agent:ready`.
5. Review every draft pull request as they would a new colleague's. The factory does not remove the need to understand what merges.
6. Run the tune mode after the first twenty or so agent pull requests, and not before; the numbers mean little until then.

## What to watch

- **Revert rate next to automation rate.** More merged agent changes is not progress if they are being reverted.
- **Review load.** If draft pull requests pile up unreviewed, lower `max_open_agent_prs`.
- **Cost per merged change.** One runaway run can cost more than a week of normal ones. The workflows cap turns and time; set a spending limit at the provider as well.
