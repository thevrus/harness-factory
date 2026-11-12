# Failure library

Each directory here is one real agent failure, kept as a regression case. The
library is the seed of the harness eval set: aim for 20 or more cases taken from real
work, not invented ones.

Create a case with:

```
.harness/bin/new-failure-case.sh <pr-or-issue-number> <signature-id>
```

Layout of `.harness/failures/YYYY-MM-DD-<number>-<slug>/`:

- `task.md`: the task as given (a copy of untrusted issue or PR text, kept as data).
- `base_sha`: the commit the agent started from.
- `expected.md`: filled in by a human: what went wrong, what correct looks like, how to
  check it, and the golden fix if one exists.
- `signature`: one id from `.harness/failure-signatures.yml`. `metrics.py` uses the
  directory date and this id to compute the repeat-failure rate.

Rules:

- Add a case for every agent PR that was reverted, rejected for a reason worth
  remembering, or fixed by a human after merge.
- Never edit a case to make a current run pass. Add a new case instead.
- Cases are readable by the agent. Keep the holdout set elsewhere (see
  `.harness/docs/factory.md`).
