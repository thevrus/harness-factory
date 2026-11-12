# Evidence rules (adapter)

The full, agent-neutral rules are in `.harness/docs/evidence.md`. Read them
before claiming any work is done. In short:

- Evidence is the verbatim final line of `.harness/bin/gates.sh`
  (`HARNESS_GATES: PASS (...)`), and `.harness/bin/smoke.sh` for core-flow
  changes, run in this session after your last edit.
- "Should work", mocked unit tests and earlier results are not evidence.
- A MISCONFIGURED or stub verdict means "not verified"; say so.
- The project `verify` skill runs both scripts; use it before every commit.
- A Stop hook (`.harness/hooks/require-evidence.sh`) blocks finishing when
  code changed since the last passing gate run.
