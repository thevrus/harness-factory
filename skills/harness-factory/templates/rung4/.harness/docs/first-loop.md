# First loop: setup, operation, rollback

Rung 4 installs one agent loop: pull request review (`.github/workflows/agent-review.yml`)
or issue triage (`.github/workflows/agent-triage.yml`). Both follow the same shape:

1. A gate or select job with no model key and read-only permissions.
2. An agent job with the model key and a read-only `GITHUB_TOKEN`. It reads untrusted
   text as files and emits JSON matching a schema in `.harness/schemas/`.
3. A publish or apply job with a write permission and no model key. It validates the JSON
   with `.harness/bin/agent-output.py` and writes at most one comment (plus labels).

The agent never holds a write token and the writing job never runs the model, so text
from an issue or pull request cannot both steer the agent and act on the repository.

## One-time human setup

Do these in the repository settings. Nothing here can be done from a committed file.

1. **Model credential.** The marked `AGENT STEP` block in the workflow names the
   credential its adapter reads. For the default adapter, add a repository secret named
   `ANTHROPIC_API_KEY` (Settings, Secrets and variables, Actions). The comment above the
   agent step lists the alternatives (subscription token, or OIDC workload identity
   federation with no stored key, which also needs `id-token: write` on the agent job
   only). Prefer an organization-owned credential with a monthly spend limit set at the
   provider. A personal token ties the loop to one person.
2. **Kill switch.** Add a repository variable `AGENT_ENABLED` with value `true`
   (Settings, Secrets and variables, Actions, Variables). Every agent workflow checks it
   before doing anything.
   - Pause: `gh variable set AGENT_ENABLED --body false`
   - Resume: `gh variable set AGENT_ENABLED --body true`
   - Harder stop: `gh workflow disable agent-review.yml` (or `agent-triage.yml`).
   Test the switch once: set it to `false`, trigger the workflow, confirm no job ran.
3. **Labels.** Create them so their color and description are deliberate:
   ```
   gh label create agent:reviewed  --description "Reviewed by the agent loop"
   gh label create agent:triaged   --description "Triaged by the agent loop"
   gh label create agent:ready     --description "Small and specified; a maintainer may start the agent"
   gh label create agent:needs-spec --description "Needs a written spec first"
   gh label create agent:needs-info --description "Needs reproduction or details"
   gh label create agent:parked    --description "Not for the agent"
   gh label create agent:triage-failed --description "Triage output was rejected; a human triages"
   ```
4. **Maintainers.** `.harness/maintainers.txt` lists the GitHub logins whose actions
   count. Edit it through a reviewed pull request.
5. **Workflow ownership.** Put `.github/workflows/` and `.harness/` in CODEOWNERS and
   require code-owner review, so a pull request cannot loosen the loop that reviews it.

## Who can trigger what

- Review runs on `pull_request_target`, so the workflow file and prompt come from the
  base branch, never from the pull request. The gate job passes only open, non-draft
  pull requests whose author has `admin` or `write` permission. A maintainer can review
  an open pull request, draft or not, with `gh workflow run agent-review.yml -f pr_number=N`;
  the same author check applies, so a dispatch does not review a pull request from an
  author without write access.
  Pull requests opened by a workflow's `GITHUB_TOKEN` (such as rung 5 agent PRs) do not
  start workflows; dispatch the review for those. A dispatch accepts a pull request
  authored by the Actions bot only from an `agent/issue-*` branch in this repository.
- Triage runs hourly and on dispatch (`gh workflow run agent-triage.yml -f issue=N`). It
  takes the oldest open issue without a triage label, one per run. If the agent's result
  is rejected (or the classify or apply job fails), the issue gets `agent:triage-failed`
  and a fixed comment, and the sweep moves on to the next issue. Triage it by hand, or
  retry with a dispatch for that issue number; a successful retry removes the label.
  `agent:triage-failed` is set by the workflow only: never add it to
  `.harness/triage-labels.txt`, or the agent could choose it.
- Labels set by triage use `GITHUB_TOKEN` and start no other workflow. `agent:ready`
  from triage is a recommendation; a maintainer decides.

## Attribution

Every comment the loop posts ends with `Agent-Run: <workflow run URL>`, and the target
gets an `agent:*` label (`agent:reviewed`, `agent:triaged`). Metrics in rung 5 count
these. Do not remove them by hand.

## Reading a run

Open the run from the `Agent-Run:` link. The agent job log shows the turn count and the
result; the uploaded artifact (`agent-review-N` or `triage-result`) holds the raw JSON;
the publish or apply job log shows the validator verdict (`agent-output: VALID` or
`INVALID` with the reason). An `INVALID` verdict means nothing was posted. Repeated
`INVALID` results on one issue or pull request are worth reading: they are often an
injection attempt.

Turn caps and `timeout-minutes` bound each run. Check the provider's usage page weekly
for the first month.

## Rollback

1. Pause: `gh variable set AGENT_ENABLED --body false`.
2. Remove: delete the workflow file in a pull request (and the `.harness/prompts/`,
   `.harness/schemas/`, `.harness/bin/agent-output.py`, `.harness/bin/strip-hidden.py`
   files if no other rung uses them). The installer's `.harness/installed.json` lists
   every file this rung wrote.
3. Revoke: delete the model secret, or disable the federation rule.
4. Labels and posted comments stay as history. Remove labels with `gh label delete` if
   wanted.
