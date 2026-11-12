#!/usr/bin/env bash
# PreToolUse guard for the agent's file-edit tools (Edit, Write, NotebookEdit).
#
# Reads the hook JSON on stdin, resolves the target path against the repo
# (absolute paths, "..", and symlinks all resolved) and checks it against
# .harness/protected-paths.txt plus the built-in defaults:
#   protected path        needs a human's approval: the adapter asks the user
#                         to confirm (unattended runs deny the edit)
#   .harness/acceptance/  blocked with exit 2, approval or not
#
# This guard only covers the agent's own file tools. A shell command that
# writes a file is not seen here; the hard layers are the permission deny
# rules, the sandbox (when enabled) and diff-guard as a required CI check.
#
# Fails closed: missing python3, unreadable input or a missing policy file all
# block, and the hook is registered with "onFailure": "block".
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
guard="$here/../bin/harness_guard.py"

# Agent adapter: stdin passes through in the agent's hook format; the
# per-agent parse/respond pair (and the rejection of an unknown agent, exit 2)
# lives in harness_guard.py ADAPTERS.
hook_io() {
  python3 "$guard" hook-path --format="$1"
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
# Only 0 (allow or ask) and 2 (block) are meaningful; anything else is a crash.
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  echo "BLOCKED by harness guard: guard crashed (exit $rc)" >&2
  exit 2
fi
exit "$rc"
