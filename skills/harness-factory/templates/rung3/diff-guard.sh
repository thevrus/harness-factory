#!/usr/bin/env bash
# diff-guard: fail when a change touches a protected path or is too large.
#
# Usage: .harness/bin/diff-guard.sh [--base REF] [--head REF] [--worktree] [--allow-protected]
#   --base REF         compare against the merge base with REF
#                      (default origin/{{default_branch}})
#   --worktree         include uncommitted and untracked files (local use)
#   --allow-protected  accept protected-path changes, but ONLY when the
#                      environment also has HARNESS_PROTECTED_APPROVED=1
#
# Protected paths: .harness/protected-paths.txt plus built-in defaults.
# Size limit: max_diff_lines in .harness/config.json; lockfiles, generated
# files and entries in .harness/generated-paths.txt are not counted.
# Policy files are read from the base ref, so a change cannot loosen the rules
# that judge it.
#
# Default is deny. In CI, .github/workflows/harness-guard.yml sets
# HARNESS_PROTECTED_APPROVED=1 only when an individual CODEOWNER other than the
# author has approved the current head commit. Setting it by hand on a local
# run proves nothing; the CI result is the one that counts.
#
# Final line: HARNESS_DIFF_GUARD: PASS|FAIL|MISCONFIGURED (...)
# Exit: 0 pass, 1 fail, 2 misconfigured (git, base ref, or config missing).
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
guard="$here/harness_guard.py"

if ! command -v python3 >/dev/null 2>&1; then
  echo "HARNESS_DIFF_GUARD: MISCONFIGURED (python3 not found)"
  exit 2
fi
if ! command -v git >/dev/null 2>&1; then
  echo "HARNESS_DIFF_GUARD: MISCONFIGURED (git not found)"
  exit 2
fi
if [ ! -f "$guard" ]; then
  echo "HARNESS_DIFF_GUARD: MISCONFIGURED ($guard missing)"
  exit 2
fi

python3 "$guard" diff "$@"
rc=$?
case "$rc" in
  0|1|2) exit "$rc" ;;
  *) echo "HARNESS_DIFF_GUARD: MISCONFIGURED (guard crashed, exit $rc)"; exit 2 ;;
esac
