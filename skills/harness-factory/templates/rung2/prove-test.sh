#!/usr/bin/env bash
# prove-test.sh: prove a new or changed test actually tests the change.
# Managed by harness-factory. Approach adapted from addyosmani/factory's
# prove-test.sh (MIT), which works on commits; this one works on the
# uncommitted working tree.
#
#   1. Set aside the non-test part of the working diff (relative to HEAD).
#   2. Run the test command: it must FAIL without the change.
#   3. Restore the change and run it again: it must PASS.
#
# Usage:
#   .harness/bin/prove-test.sh [--test-path PATH]... -- <test command...>
# Without --test-path, files are classed as tests by common naming
# (test/, tests/, __tests__/, *.test.*, *.spec.*, test_*, *_test.*).
#
# Safety: refuses to run when the index differs from HEAD or a merge/rebase
# is in progress (it could not restore those faithfully). Every changed
# non-test file is copied to a backup directory first and copied back on
# every exit path (trap), then the tree hash is compared with the hash taken
# before. On a mismatch the backup is kept and its path printed.
# Caveat: caches with one-second mtime resolution can still go stale if the
# whole run takes under a second; disable them for the test command if so.
#
# Last line: HARNESS_PROOF: PROVEN|NOT_PROVEN|MISCONFIGURED (<detail>)
# Exit: 0 PROVEN, 1 NOT_PROVEN, 2 MISCONFIGURED, 3 restore problem.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh disable=SC1091
. "$SELF_DIR/_lib.sh"

verdict() { printf 'HARNESS_PROOF: %s (%s)\n' "$1" "$2"; exit "$3"; }
usage() { echo "usage: $0 [--test-path PATH]... -- <test command...>" >&2; verdict MISCONFIGURED "usage" 2; }

test_paths=()
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
  [ "$1" = "--test-path" ] && [ "$#" -ge 2 ] || usage
  test_paths+=("${2%/}")
  shift 2
done
[ "${1:-}" = "--" ] && [ "$#" -ge 2 ] || usage
shift

ROOT="$(harness_root)" || verdict MISCONFIGURED "not inside a git repository" 2
cd "$ROOT" || verdict MISCONFIGURED "cannot cd to repo root" 2
git rev-parse --verify -q HEAD >/dev/null || verdict MISCONFIGURED "no HEAD commit to compare against" 2

for p in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  [ -e "$(git rev-parse --git-path "$p")" ] && verdict MISCONFIGURED "a merge, rebase or cherry-pick is in progress" 2
done
if ! git diff --cached --quiet; then
  verdict MISCONFIGURED "index differs from HEAD; unstage (git restore --staged .) or commit first, it cannot be restored safely" 2
fi

is_test() {
  local path="$1" tp
  if [ "${#test_paths[@]}" -gt 0 ]; then
    for tp in "${test_paths[@]}"; do
      [ "$path" = "$tp" ] && return 0
      case "$path" in "$tp"/*) return 0 ;; esac
    done
    return 1
  fi
  case "$path" in
    test/*|tests/*|*/test/*|*/tests/*|__tests__/*|*/__tests__/*|*.test.*|*.spec.*|test_*|*/test_*|*_test.*|*_spec.*) return 0 ;;
  esac
  return 1
}

tests=()
code=()
while IFS= read -r -d '' path; do
  case "$path" in .harness/.state/*) continue ;; esac
  if is_test "$path"; then tests+=("$path"); else code+=("$path"); fi
done < <(git diff --name-only -z HEAD; git ls-files -o --exclude-standard -z)

[ "${#tests[@]}" -gt 0 ] || verdict MISCONFIGURED "no new or changed test file in the working tree" 2
[ "${#code[@]}" -gt 0 ] || verdict MISCONFIGURED "no non-test change to set aside" 2

BEFORE="$(harness_tree_hash)"
BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/harness-proof.XXXXXX")" || verdict MISCONFIGURED "cannot create backup dir" 2
present=()
for path in "${code[@]}"; do
  if [ -e "$path" ] || [ -L "$path" ]; then
    if ! { mkdir -p "$BACKUP/$(dirname "$path")" && cp -P -p "$path" "$BACKUP/$path"; }; then
      rm -rf "$BACKUP"
      verdict MISCONFIGURED "could not back up $path" 2
    fi
    present+=(1)
  else
    present+=(0)
  fi
done

reverted=0
restore() {
  [ "$reverted" -eq 1 ] || return 0
  local i ok=1
  for i in "${!code[@]}"; do
    rm -f -- "${code[$i]}"
    if [ "${present[$i]}" -eq 1 ]; then
      # cp -p keeps the mode; touch gives a fresh mtime so build caches keyed
      # on mtime (make, bytecode) do not reuse output from the reverted run
      if mkdir -p "$(dirname "${code[$i]}")" && cp -P -p "$BACKUP/${code[$i]}" "${code[$i]}"; then
        [ -L "${code[$i]}" ] || touch "${code[$i]}"
      else
        ok=0
      fi
    fi
  done
  reverted=0
  if [ "$ok" -eq 1 ] && [ "$(harness_tree_hash)" = "$BEFORE" ]; then
    rm -rf "$BACKUP"
    return 0
  fi
  echo "prove-test: RESTORE MISMATCH. Your changes are backed up in $BACKUP" >&2
  printf 'HARNESS_PROOF: MISCONFIGURED (restore mismatch; backup kept at %s)\n' "$BACKUP"
  exit 3
}
trap 'restore' EXIT
trap 'restore; exit 130' INT
trap 'restore; exit 143' TERM HUP

# Set the change aside: tracked files go back to HEAD, new files are removed.
reverted=1
for path in "${code[@]}"; do
  if git cat-file -e "HEAD:$path" 2>/dev/null; then
    git checkout -q HEAD -- "$path" || { echo "prove-test: could not revert $path" >&2; exit 3; }
  else
    rm -f -- "$path"
  fi
done

echo "=== without the change (expect FAIL): $* ==="
"$@"
rc_without=$?
restore

echo "=== with the change (expect PASS): $* ==="
"$@"
rc_with=$?

detail="tests=${#tests[@]} set_aside=${#code[@]} exit_without=$rc_without exit_with=$rc_with"
if [ "$rc_without" -eq 127 ] || [ "$rc_with" -eq 127 ]; then
  verdict MISCONFIGURED "test command not found; $detail" 2
elif [ "$rc_without" -eq 0 ]; then
  verdict NOT_PROVEN "test passes without the change; $detail" 1
elif [ "$rc_with" -ne 0 ]; then
  verdict NOT_PROVEN "test fails with the change; $detail" 1
fi
verdict PROVEN "$detail" 0
