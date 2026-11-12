# Tune a harness from evidence

A harness improves when each correction a person makes becomes something durable: a check, a rule, or a regression case. This skill proposes those changes. It never applies them unreviewed, because a loop that rewrites its own rules can be steered by whoever writes the comments it learns from.

Everything below reads from `.harness/` in the target repository. If `.harness/ledger.jsonl` does not exist, the repository has no harness yet; point the user to `the advance mode`.

## 1. Read the record

- `.harness/ledger.jsonl`: one JSON line per audit, install and metrics run. Look at the trend, not the latest line.
- If `.harness/bin/metrics.py` is installed (rung 5), run it for the window the user wants. It prints each metric with its denominator and a human-PR baseline.

When reporting metrics:

- Never give the automation rate without the revert rate beside it. Shipping more changes that get reverted is not progress.
- A `null` metric means the data is missing. Say what is missing; do not treat it as zero.
- Small denominators mean little. Under about 20 agent pull requests, report counts, not percentages.

## 2. Record a failure

When the user reports that an agent got something wrong, or you find it in a pull request:

1. Classify the root cause before proposing anything: missing instruction, missing context, missing check, weak gate, wrong tool permission, ambiguous task, or model limit.
2. If `.harness/bin/new-failure-case.sh` is installed, record the case with it. Otherwise add an entry to `.harness/rules-inbox.md` by hand: date, link, one-line description, root cause.
3. Do not repair the original output as part of this step. Fixing the output and fixing the harness are separate changes.

## 3. Propose a harness change

Promote a correction only when it has occurred at least twice, independently. One occurrence is an observation.

Choose the strongest form that fits, in this order:

1. **A check** (a gate, a lint rule, a guard). It cannot be ignored and costs no context.
2. **A regression case** in the failure library. It shows whether the fix holds.
3. **A rule** in `AGENTS.md` or `.claude/rules/`. Use this only for facts an agent cannot find in the code and a script cannot enforce.

For every proposal state: the evidence (links to the occurrences), the exact change, the file, and how to tell later whether it worked.

Rules for the change itself:

- Patch one item. Never regenerate or rewrite a whole instructions file.
- A rule names the concrete situation and the action. "Be careful with dates" changes nothing; "a launch date claim needs the primary source, and announcement date is not availability date" does.
- Changes to `AGENTS.md`, `CLAUDE.md`, `.claude/**` and `.harness/**` are protected paths. Deliver them as a pull request for a maintainer, never as a direct commit.
- Only corrections from the logins in `.harness/maintainers.txt` count as evidence.

## 4. Prune

Instructions accumulate and each one is paid for on every turn. On a periodic review, or after a model release:

- List rules with no occurrence of their failure since they were added.
- For each, propose removal, or state the evidence that it still earns its place.
- After a new model release, re-test whether each harness component is still needed, one at a time. Components encode assumptions about what the model cannot do, and those go stale.
- Root instruction files over about 200 lines are a finding in themselves.

## 5. Report

Give the user: the trend since the last review, the failures recorded, the changes proposed (each with evidence), and the removals proposed. If nothing has repeated, say that and propose nothing.
