#!/usr/bin/env bash
# PreToolUse guard for shell commands: blocks merges, approvals and pushes to
# protected branches (the default branch from .harness/config.json, plus main
# and master).
#
# Blocked forms it recognizes, after splitting compound commands, stripping
# wrappers (env, timeout, nohup, sudo, xargs...) and recursing into sh -c / eval:
#   gh pr merge ...                      gh pr review --approve
#   gh api .../pulls/N/merge, .../merges, GraphQL mergePullRequest and auto-merge
#   gh api with a write method on git/refs, branch protection or rulesets
#   gh repo sync
#   git push <remote> <default>          git push <remote> x:<default>
#   git push <remote> HEAD:<default>     git push <remote> refs/heads/<default>
#   git push <remote> +<default>, :<default>, --delete <default>
#   git push with no refspec (or HEAD) while on the default branch, including
#     after git checkout/switch <default> (or -B/-C <default>) earlier in the
#     same command line; cd is followed the same way
#   git push --all / --mirror / --branches / --prune
#   git push with a $VAR or glob refspec  git -c <push/alias/url config> push
#   git send-pack
# Force pushes are covered because every push to a protected branch is blocked.
#
# Switching on or starting unattended automation is a person's call, so these
# get "ask" where the agent supports it (Claude, Copilot), a block elsewhere:
#   gh variable set/delete AGENT_ENABLED (or set -f <file>)
#   gh issue edit --add-label agent:ready, gh issue create --label agent:ready
#     (also gh pr edit/create)
#   gh workflow run/enable <agent-*>
#   gh api with a write method on .../variables, on an issue's labels adding
#     agent:ready, or on actions/workflows/agent-*/dispatches or /enable
#
# It also blocks shell writes to never-agent-editable paths (HARD_BLOCKED in
# harness_guard.py, .harness/acceptance/): a redirect target there, or any
# argument that resolves there (or to a parent such as .harness, or a glob or
# $VAR that may) on a command not on the read-only list (cat, ls, head, tail,
# grep, rg, wc, diff, find without -delete/-exec, test, git diff/log/show/
# status...). `cd` is followed within one command line. Same caveat: a script
# file or an alias can still reach them; the CI diff guard is the backstop.
#
# Shell writes to the other protected paths (protected-paths.txt plus the
# built-in defaults: .harness/, .claude/, .codex/, CLAUDE.md, lockfiles...)
# get the same verdict as an edit tool: "ask" where the agent supports it
# (Claude, Copilot), a block elsewhere. Running a protected script
# (.harness/bin/gates.sh, bash .harness/bin/smoke.sh) is not a write, nor are
# git add/commit. Here-document bodies, echo/printf arguments, git commit -m/-F
# and gh --title/--body values are text, not commands or paths.
#
# THIS IS BEST EFFORT. Text inspection cannot see every route (an alias set
# earlier, a script file, an API client in another language). The hard layer is
# the permission deny rules plus GitHub branch protection (a ruleset with no
# bypass for the agent's identity); see .harness/docs/branch-protection.md.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
guard="$here/../bin/harness_guard.py"

# Agent adapter: stdin passes through in the agent's hook format; the
# per-agent parse/respond pair (and the rejection of an unknown agent, exit 2)
# lives in harness_guard.py ADAPTERS.
hook_io() {
  python3 "$guard" hook-bash --format="$1"
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "BLOCKED by harness guard: MISCONFIGURED (python3 not found)" >&2
  exit 2
fi
if [ ! -f "$guard" ]; then
  echo "BLOCKED by harness guard: MISCONFIGURED ($guard missing)" >&2
  exit 2
fi

# Usage: <hook> [--agent ID]   (hook payload on stdin; default ID claude)
AGENT="claude"
[ "${1:-}" = "--agent" ] && [ -n "${2:-}" ] && AGENT="$2"

hook_io "$AGENT"
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  echo "BLOCKED by harness guard: guard crashed (exit $rc)" >&2
  exit 2
fi
exit "$rc"
