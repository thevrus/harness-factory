# What counts as evidence

Managed by harness-factory. A claim that work is done, fixed or safe needs
evidence from this session. Quote it; do not paraphrase it.

## Counts

- The final `HARNESS_GATES: PASS (...)` line from `.harness/bin/gates.sh`,
  run in this session on the current tree, quoted verbatim.
- The final `HARNESS_SMOKE: PASS (...)` line from `.harness/bin/smoke.sh`
  for anything that touches the core flow.
- `HARNESS_PROOF: PROVEN (...)` from `.harness/bin/prove-test.sh` for a new
  or changed test: it failed without the change and passed with it.
- Output of a command you ran against the real system (request and response,
  CLI output, log line), with the command shown.

## Does not count

- "Should work", "looks correct", "I am confident". Confidence is what
  evidence checks.
- A passing unit test that mocks the thing you changed. It proves the mock,
  not production behavior.
- A result from an earlier session, another branch, or before your last edit.
- A summary of a gate result instead of the verdict line itself.
- Exit code 0 from a gate reported as MISCONFIGURED or a stub
  (`HARNESS_STUB`). Say plainly that it is not verified.

## When you cannot get evidence

State what you could not verify and why, and what a human should run. An
honest "not verified" is acceptable; an unbacked "done" is not.
