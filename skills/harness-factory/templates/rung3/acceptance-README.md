# Acceptance scenarios

This directory holds the acceptance scenarios a change is judged against:
the behaviour a human has decided the product must have, written down so a
check can confirm it.

Rules:

- **Humans write and change these files.** An agent may read them, run them
  and report on them. It may not create, edit, move or delete them.
- **They are write-protected from the implementing agent.** An agent that can
  edit the tests it is judged on will, sooner or later, make them pass by
  changing the tests instead of the code. Protection is enforced, not
  requested:
  - `.harness/acceptance/**` is listed in `.harness/protected-paths.txt`, so the
    file-edit hook blocks the agent's edit tools here;
  - the agent's permission settings deny edits under this path;
  - `diff-guard` fails any pull request that touches this directory unless a
    CODEOWNER has approved it.
- If a scenario is wrong, the agent should say so in its report and stop. A
  human then fixes the scenario in a separate change.

Suggested layout (adapt to your stack):

```
.harness/acceptance/
  README.md            this file
  <feature>.md         scenario in plain language: given / when / then
  <feature>.test.*     optional executable form, run by .harness/bin/gates.sh
```

Keep each scenario about observable behaviour (inputs and outputs a user or
caller sees), not about how the code is structured.
