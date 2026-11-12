---
name: verify
description: Run the repo's harness gates and core-flow smoke check and report their verbatim verdict lines. Use before every commit, before saying work is done, fixed or ready, and whenever asked to verify, test or check changes.
allowed-tools: Bash(.harness/bin/gates.sh), Bash(.harness/bin/gates.sh *), Bash(.harness/bin/smoke.sh), Bash(.harness/bin/smoke.sh *)
---

# verify

Managed by harness-factory. Evidence rules: `.harness/docs/evidence.md`.

1. Run `.harness/bin/gates.sh` from the repo root. Do not pass flags or edit
   `.harness/gates.conf` to make it pass.
2. If any changed file is on the core flow, or you are unsure, run
   `.harness/bin/smoke.sh`.
3. Report each script's final line verbatim in a code block:
   `HARNESS_GATES: ...` and, if run, `HARNESS_SMOKE: ...`.
   Never summarize or reword them, and never report a result that
   contradicts them.
4. Act on the verdict:
   - `PASS`: you may commit or report done, quoting the lines.
   - `FAIL`: do not commit. Fix the cause, then run this skill again.
   - `MISCONFIGURED`: do not commit as verified. Report which gate could
     not run and what is missing (tool, command, stub). Do not work around
     it by changing the gate.
5. For a new or changed test, also run
   `.harness/bin/prove-test.sh -- <test command>` and quote its
   `HARNESS_PROOF:` line.
