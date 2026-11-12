# Audit a repository's agent harness

The audit is a script, not your judgment. Run it, report what it says, and do not re-score the repo yourself.

## Run it

```bash
python3 "$HF/hf_audit.py" --repo .
```

- Use the path in `$ARGUMENTS` in place of `.` when one is given.
- The default run is static: it reads files, executes nothing from the repository, and writes nothing.
- Add `--offline` to skip the one network call (the GitHub branch-protection lookup through `gh`).
- Add `--json <file>` when another step needs the result. Write that file outside the repository.

## The `--run` flag executes repository code

`--run` executes `.harness/bin/gates.sh`, `.harness/bin/smoke.sh` and the workflow linter from the repository being audited. Those scripts run the project's own test and start commands.

- Pass `--run` only when the user asked for it or agreed to it in this conversation.
- Never pass it for a repository the user does not own or has just cloned from an unknown source.
- Without it, the checks that need execution report `not_measured`. That is the correct result, not a failure of the audit.

## Read the result

| Status | Meaning |
|---|---|
| `pass` | Verified mechanically |
| `fail` | Verified missing or wrong; the row carries a fix |
| `not_measured` | Needs `--run` |
| `unknown` | The script could not decide (for example `gh` is not authenticated) |
| `na` | Does not apply to this repository |

A rung is achieved only when every required check of that rung and of every rung below it is `pass` or `na`. `not_measured` and `unknown` block a rung on purpose: a harness that has not been proven to work does not count.

## Report

Lead with the rung achieved and the next rung. Then list the blockers for the next rung only, each with its fix. Leave the higher rungs out unless the user asks; they cannot be installed yet.

Say plainly which blockers a tool can fix and which need a person (writing tests, turning on branch protection, supplying facts about the system).

## Do not

- Do not fix anything from this skill. Fixing is `the advance mode`.
- Do not mark a check as passing because the code "looks fine".
- Do not summarize the repository or generate documentation from it.
