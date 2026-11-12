# Factory operating guide

Rung 5 adds the implement lane (`agent-implement.yml`), the monitor (`agent-monitor.yml`),
metrics, the failure library and the rules inbox. Agents propose; humans merge, promote
rules and change the harness.

## State machine

| Label | Meaning | Who may set it |
|---|---|---|
| (none) | New issue | anyone |
| `agent:triaged` | The triage loop looked at it | triage apply job |
| `agent:needs-info`, `agent:needs-spec`, `agent:parked` | Not ready for the agent | triage apply job, maintainers |
| `agent:ready` | Approved for the agent | maintainers (triage may suggest it) |
| `agent:in-progress` | Claimed: branch `agent/issue-N` exists | claim job |
| `agent:authored` | On the draft PR the agent wrote | publish job |
| `agent:reviewed` | On a PR the review loop commented on | review publish job |
| `agent:incident` | Regression issue filed by the monitor | monitor file job |

Transitions that start work:

- `agent:ready` applied by a login in `.harness/maintainers.txt`, or
  `gh workflow run agent-implement.yml -f issue=N` by such a login, starts the implement
  lane. The label set by the triage loop does not: it is written with the job token,
  which starts no workflow, and its sender is not a maintainer.
- The claim is the branch `agent/issue-N`, pushed without force. If it exists, the run
  stops. A failed run leaves the branch and sets `agent:parked`; delete the branch to
  allow another run. This stops retry loops without a human looking.
- The lane opens a DRAFT pull request only. Merging is a human decision, protected by
  branch rules (require one approval; the job token must not count as an approver).

Limits:

- Work in progress: no new claim while `max_open_agent_prs` (`.harness/config.json`,
  default 3) agent PRs are open. Review capacity is the bottleneck.
- Stall rule: the agent is instructed to stop after `max_fix_attempts` (default 2) failed
  gate cycles; the turn and time limits are the hard caps. Nothing in the workflow counts
  attempts. A stopped run still opens the draft PR, titled `[gates failing]`.
- Each job has `timeout-minutes`; each agent step has a turn cap (`--max-turns`).
- Model key exposure: the implement job lets the agent run `.harness/bin/gates.sh`, which
  runs agent-written tests in the same job as the Claude process that holds the model
  key. `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is the only barrier between that code and the
  key. Give this lane a dedicated, spend-capped key and rotate it if a run looks wrong.
- Approved text only: the gate refuses an issue whose title or body was edited after the
  `agent:ready` label was applied (or after the dispatch), and the implement job stops if
  the text changed between the gate and its own fetch. Re-apply the label to approve the
  new text.

## Required setup beyond rung 4

1. Settings, Actions, General: allow GitHub Actions to create pull requests (the publish
   job opens the draft with the job token). Keep branch protection requiring a human
   approval so this setting cannot turn into self-approval.
2. Repository variable `AGENT_CI_WORKFLOW`: file name of the CI workflow (for example
   `ci.yml`) with a `workflow_dispatch` trigger. Pull requests and pushes made with the
   job token start no workflows, so the publish job dispatches CI on the agent branch.
   Leave it unset and CI runs only when a human pushes or dispatches.
3. Labels: `agent:in-progress`, `agent:authored`, `agent:incident` (see
   `first-loop.md` for the others).
4. Toolchain: fill in the two `TOOLCHAIN SETUP` blocks in
   `.github/workflows/agent-implement.yml` (in the `implement` and `verify-gates`
   jobs). Add the language setup actions your tests need (for example
   `actions/setup-node`, pinned to a full SHA) and set `commands.setup` in
   `.harness/config.json` (for example `npm ci` or `pip install -r requirements.txt`);
   each block runs it before the gates. The agent's shell cannot install anything, so
   without this the gates fail and every pull request is titled `[gates failing]`.

## Pause everything

`gh variable set AGENT_ENABLED --body false`. Every agent workflow checks it first; jobs
already queued fail at their first step. To stop one lane only, `gh workflow disable
<file>`. Open draft PRs stay; close them by hand if needed.

## Metrics

`python3 .harness/bin/metrics.py --window-days 7` prints the metrics with numerators,
denominators and a human-PR baseline, and appends a `kind:"metrics"` line to
`.harness/ledger.jsonl`. Automation rate is always shown with revert rate. A null value
carries its reason; it is not zero. Cost comes from `Agent-Cost-USD:` lines that the
agent step adapter may write; runs that opened no PR are not counted, so read it as a
lower bound. The monitor runs the same script daily and files at most one
`agent:incident` issue for reverts of agent PRs or failing checks on the default branch.

## Promote a rule

1. Run `python3 .harness/bin/mine-corrections.py --repo OWNER/NAME` (weekly is enough).
   It reads only maintainer reviews and updates `.harness/rules-inbox.md`.
2. A candidate marked PROMOTION CANDIDATE has 2 or more independent PRs. Open a PR that
   adds, in this order of preference, a deterministic check, then an instruction line,
   and records a failure case with `new-failure-case.sh`. A human reviews it.
3. Never let an agent edit its own instructions, gates or this directory. The verify
   job rejects any agent patch under `.harness/` or `.github/`.

## Monthly prune

1. List rules and checks added in the last quarter (git log on the instruction files and
   `.harness/`).
2. For each, look up its failure signature: if it has had no occurrences in 60 days of
   metrics and inbox runs, try removing it on a branch and run the failure cases tagged
   with that signature. If they still pass for two consecutive months, delete the rule
   in a reviewed PR.
3. Keep root instruction files under 200 lines.
4. Re-check the action pins in the workflows (40-character SHAs) and update them in one
   reviewed PR.

## Holdout set

Keep a holdout set of tasks with expected outcomes outside the agent-readable tree: a
separate private repository or an encrypted CI artifact, read only by an eval job. The
agent must never see it, or it stops measuring anything. `.harness/failures/` is the
opposite: it is visible on purpose, as the regression library.

## Swapping the agent

Each agent workflow has one block marked `AGENT STEP (adapter: ...)`. A different agent
replaces that one step and honors the contract written above it (inputs, tool limits,
where the JSON result goes). Gates, claim, verify, publish, metrics and labels stay.
