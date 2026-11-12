# Branch protection and ownership (set by hand)

The hooks and permission rules inside an agent session are best effort. The
hard layer is GitHub itself: a ruleset on the default branch that nobody,
including the agent's identity, can bypass. These settings are a human
decision, so the harness documents them instead of applying them.

## 1. Ruleset on `{{default_branch}}`

Settings > Rules > Rulesets > New branch ruleset:

| Setting | Value |
| --- | --- |
| Enforcement status | Active |
| Bypass list | Empty. No apps, bots or agent accounts. |
| Target branches | Include default branch |
| Restrict deletions | On |
| Block force pushes | On |
| Require a pull request before merging | On |
| Required approvals | 1 or more |
| Dismiss stale pull request approvals when new commits are pushed | On |
| Require review from Code Owners | On |
| Require approval of the most recent reviewable push | On |
| Require status checks to pass | On, with `harness-guard` and `harness-gates` |
| Require branches to be up to date before merging | On |
| Require signed commits | Recommended |

Also, under Settings > Actions > General:

- Workflow permissions: **Read repository contents and packages permissions**.
- **Allow GitHub Actions to create and approve pull requests: off.**
- Fork pull request workflows: require approval for outside collaborators.

And under Settings > Code security: secret scanning and push protection on.

## 2. Check it with `gh api`

```sh
# Rules in effect on the default branch (rulesets and their parameters)
gh api repos/{owner}/{repo}/rules/branches/{{default_branch}}

# Expect: deletion, non_fast_forward, pull_request with
#   require_code_owner_review: true, dismiss_stale_reviews_on_push: true,
#   require_last_push_approval: true, required_approving_review_count >= 1,
# and required_status_checks listing harness-guard and harness-gates.
gh api repos/{owner}/{repo}/rules/branches/{{default_branch}} \
  --jq '.[] | {type, parameters}'

# Rulesets and their bypass actors (bypass_actors should be empty)
gh api repos/{owner}/{repo}/rulesets --jq '.[] | {id, name, enforcement}'
gh api repos/{owner}/{repo}/rulesets/RULESET_ID --jq '{enforcement, bypass_actors}'

# Classic branch protection, if you use it instead of rulesets
gh api repos/{owner}/{repo}/branches/{{default_branch}}/protection

# Actions: default token read-only, Actions cannot approve PRs
gh api repos/{owner}/{repo}/actions/permissions/workflow
# Expect: default_workflow_permissions "read", can_approve_pull_request_reviews false

# Secret scanning and push protection
gh api repos/{owner}/{repo} --jq '.security_and_analysis'

# CODEOWNERS syntax errors and unknown owners
gh api repos/{owner}/{repo}/codeowners/errors
```

`gh api` fills `{owner}` and `{repo}` from the current checkout.

## 3. CODEOWNERS (suggested, not installed)

Who owns what is a human decision, so the harness does not write this file.
Create `.github/CODEOWNERS` with individual owners (`@login`), replacing the
placeholders:

```
# Harness and agent configuration
/.harness/            @OWNER
/.github/workflows/   @OWNER
/.github/CODEOWNERS   @OWNER
/.claude/             @OWNER
/CLAUDE.md            @OWNER
/AGENTS.md            @OWNER
/.mcp.json            @OWNER

# Add the repo-specific protected areas from .harness/protected-paths.txt
# /migrations/        @OWNER
# /src/auth/          @OWNER
# /src/billing/       @OWNER
# /infra/             @OWNER
```

## 4. How a protected-path change gets merged

1. diff-guard fails any pull request that touches a protected path.
2. A CODEOWNER other than the author reviews and approves the **current head
   commit**.
3. The approval re-runs `harness-guard`. Its approval step finds an
   individual owner from the base branch's CODEOWNERS among the approvers of
   that commit and sets `HARNESS_PROTECTED_APPROVED=1` for the diff-guard step
   only. diff-guard then accepts the protected paths (the size limit still
   applies).
4. A new push makes the approval stale, and the check fails again.

Limits: team owners (`@org/team`) cannot be resolved with the workflow's
read-only token, so only individual owners count for step 3. Per-file
ownership, including teams, is enforced by the ruleset's "Require review from
Code Owners". Setting `HARNESS_PROTECTED_APPROVED=1` on a local run proves
nothing; only the CI result counts.
