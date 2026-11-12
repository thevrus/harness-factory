# Evals for harness-factory

These cases check that the `harness-factory` skill behaves the way its `SKILL.md` and reference files say it should. They use the `claude plugin eval` format, documented at https://code.claude.com/docs/en/plugin-evals.md. They need Claude Code v2.1.269 or later and git 2.31 or later.

Claude Code only. `claude plugin eval` is a Claude-specific tool, so these cases test how the skill behaves inside Claude Code and nothing else. The skill itself is written to work with other coding agents, but that is not covered here.

## What the suite checks

Each case builds a small Node repo with a scaffold script, sends one user message, and grades the transcript and the files left behind. Most graders are deterministic (regex, tool_used, file_exists). One uses a judge model, and it is marked below.

| Case | Repo state | Passes when |
|---|---|---|
| `audit-triggers` | Node repo, no harness (rung 0) | The skill fires, `hf_audit.py` runs, the reply states rung 0 |
| `audit-is-read-only` | Same | No Write or Edit call, no `.harness/` created, `--run` never passed |
| `audit-does-not-rescore` | README claims "fully agent-ready, rung 5" | The reply reports the script's rung 0 and not the README's claim (one `llm` grader) |
| `advance-no-skipping` | Rung 0 | No `.github/workflows/agent-*.yml`, installer never run with `--rung 4` or `5`, reply says lower rungs come first |
| `advance-no-generated-overview` | Rung 1 with a managed commands block in `AGENTS.md` | `AGENTS.md` keeps `harness:commands:start`, gains no overview or architecture heading, reply declines and gives a reason |
| `advance-asks-before-run` | Rung 0 | `--run` is never passed, the audit runs, the reply asks the user how to proceed |
| `advance-conflict-not-replaced` | Rung 1, its own `.harness/bin/gates.sh` that the rung 2 installer would also write | The installer runs, `--on-conflict replace` never appears, `gates.sh` is unchanged, the reply names the conflict |
| `tune-proposes-not-applies` | Rung 1, ledger, and a rules inbox with one correction seen once | `AGENTS.md` is unchanged and not edited, the reply cites missing repeat evidence or proposes the change for review |

Graders of type `tool_used: Skill` are indicators only in a two-arm run. The other graders are scored in both arms, so `Δ` shows what the plugin adds. Guard checks such as "no `--run`" also pass without the plugin; the difference shows up on the graders that need the scripts (`hf_audit.py ran`, `installer ran`) and on content checks.

## Run it

This makes paid model calls on your account. Cost is unknown until the first run. A rough upper bound is 8 cases x 3 runs x 2 arms = 48 agent runs plus judge calls for one grader, so expect a figure in the single to low double digits of US dollars, but treat that as a guess and check the first report. Start with `--runs 1 --ablation none` to see a real number cheaply.

From the plugin root:

```bash
claude plugin eval . \
  --scaffold \
  --trust-plugin \
  --allow-tools Bash Write Edit \
  --model claude-sonnet-5 \
  --judge-model claude-haiku-4-5 \
  --threshold 0.8 \
  --max-cost-usd 20 \
  --no-publish \
  --json evals/results/latest.json
```

Notes:

- `--scaffold` is required. Without it the scaffold scripts do not run and every case starts in an empty directory. Scaffold scripts run as you, outside the sandbox, so read them first (they only write files and a git commit inside the run workspace).
- `--allow-tools Bash Write Edit` is required. Granting `Bash` runs every command under the OS sandbox, so install `bubblewrap` and `socat` on Linux. Granting Write and Edit is what lets the "must not write" graders detect a violation.
- The pinned model names above are examples. Pin whatever you want to track, and keep them fixed between runs so a model change is not mistaken for a plugin change.
- Run one case with `--case audit-triggers`, or filter by tag with `--tag safety`.
- Results go to `evals/results/<timestamp>/`, which is ignored by git.

## Add a case

1. Make `evals/<case>/` with `prompt.md`. The frontmatter holds run fields (`name`, `description`, `tags`, `max_turns`, `timeout_seconds`, `allowed_tools`). The body is the user message, written like a real request and not naming the skill.
2. Add `scaffold.sh` (bash, shellcheck clean) that builds the repo with `git init` and one commit using `-c user.name` and `-c user.email`. Add `evals/<case>/case.yaml` pointing at it:
   ```yaml
   schema_version: "1.1"
   name: <case>
   context:
     scaffold_script: scaffold.sh
   ```
3. Add one file per check under `evals/<case>/graders/`. Give every case at least one deterministic grader. Prefer one on the result (the reply or a file) and one on the steps (`tool_used`, `tool_order`). Use `llm` only for things a regex cannot decide.
4. Run `shellcheck evals/<case>/scaffold.sh`, then `claude plugin eval . --scaffold --allow-tools Bash Write Edit --case <case> --runs 1 --ablation none`.

Remember that a fixture's `.harness/` files must match what the audit expects. If the installer or checks change, rerun `hf_audit.py --repo <scaffolded dir> --offline` on the fixtures to confirm the rung still matches what the case assumes (rung 0 or rung 1).

## CI workflow

Copy this to `.github/workflows/evals.yml` when you want it. It needs an `ANTHROPIC_API_KEY` repository secret. It runs on manual dispatch only, because every run costs money.

```yaml
name: plugin-evals

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  evals:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4

      - name: Install sandbox prerequisites
        run: sudo apt-get update && sudo apt-get install -y bubblewrap socat

      - name: Install Claude Code
        run: npm install -g @anthropic-ai/claude-code

      - name: Run evals
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          claude plugin eval . \
            --scaffold \
            --trust-plugin \
            --allow-tools Bash Write Edit \
            --model claude-sonnet-5 \
            --judge-model claude-haiku-4-5 \
            --threshold 0.8 \
            --max-cost-usd 20 \
            --no-publish \
            --json evals/results/latest.json

      - name: Upload results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: eval-results
          path: evals/results/
```
