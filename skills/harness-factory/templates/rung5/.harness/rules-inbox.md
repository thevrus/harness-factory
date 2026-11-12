# Rules inbox

Candidates collected by `.harness/bin/mine-corrections.py` from maintainer reviews on
agent pull requests. This file proposes; it does not change agent behavior.

Promotion: when a candidate has 2 or more independent occurrences (distinct PRs), open a
pull request that adds a deterministic check first (a gate, diff-guard rule or linter rule)
and an instruction line second, plus a failure case under `.harness/failures/`. A human
reviews and merges it. Patch one item at a time; never rewrite the instruction files.

Notes written outside the generated block below are kept.

<!-- mine-corrections:start -->
No run yet.
<!-- mine-corrections:end -->
