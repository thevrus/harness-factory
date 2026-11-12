#!/usr/bin/env bash
# gates.sh: the harness's deterministic verdict on the current working tree.
# Managed by harness-factory. Gates are listed in .harness/gates.conf.
#
# Verdict design credit: the fail-closed MISCONFIGURED state and the single
# machine-readable last line follow addyosmani/factory's gates.sh (MIT). This
# is an independent implementation driven by a config file instead of stack
# detection.
#
# Exit: 0 PASS, 1 FAIL (a gate ran and failed), 2 MISCONFIGURED (a required
# gate could not run: empty command, stub (see gates.conf), missing tool, or no
# required gates).
# The last line of output is always:
#   HARNESS_GATES: PASS|FAIL|MISCONFIGURED (<summary>)
# Quote that line verbatim as evidence; never paraphrase it.
#
# On PASS it writes .harness/.state/gates-passed with the current tree hash,
# so a hook can tell whether the gates ran on exactly this state.
#
# Env: HARNESS_GATE_TIMEOUT seconds per gate (default 1800).

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh disable=SC1091
. "$SELF_DIR/_lib.sh"

verdict() {
  printf 'HARNESS_GATES: %s (%s)\n' "$1" "$2"
  exit "$3"
}

ROOT="$(harness_root)" || verdict MISCONFIGURED "not inside a git repository" 2
cd "$ROOT" || verdict MISCONFIGURED "cannot cd to repo root" 2

CONF=".harness/gates.conf"
TIMEOUT="${HARNESS_GATE_TIMEOUT:-1800}"
[ -f "$CONF" ] || verdict MISCONFIGURED "missing $CONF" 2

PASSED=""
FAILED=""
SKIPPED=""
MISCONF=""
N_REQUIRED=0
# add LISTVAR ITEM: append to a comma list without eval (conf text is data)
add() { local cur="${!1}"; printf -v "$1" '%s' "${cur}${cur:+,}$2"; }

# is_stub CMD: true when CMD (already trimmed) is a placeholder that cannot
# fail, so its exit status says nothing about the tree. Detected forms are the
# ones listed in the gates.conf header; keep the two in sync.
is_stub() {
  local c="$1" seg first
  [[ "$c" == *HARNESS_STUB* ]] && return 0
  [[ "$c" =~ ^(true|:|exit[[:space:]]+0)$ ]] && return 0
  [[ "$c" =~ ^:[[:space:]] ]] && return 0
  [[ "$c" =~ (\|\||\;)[[:space:]]*(true|:|exit[[:space:]]+0)$ ]] && return 0
  # only echo/printf, in every command of a list or pipeline
  c="${c//&&/;}"; c="${c//||/;}"; c="${c//|/;}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"
    [ -z "$seg" ] && continue
    first="${seg%%[[:space:]]*}"
    case "$first" in echo|printf) ;; *) return 1 ;; esac
  done <<< "${c//;/$'\n'}"
  return 0
}

while IFS='|' read -r name req cmd || [ -n "$name" ]; do
  # trim surrounding whitespace
  name="${name#"${name%%[![:space:]]*}"}"; name="${name%"${name##*[![:space:]]}"}"
  req="${req#"${req%%[![:space:]]*}"}"; req="${req%"${req##*[![:space:]]}"}"
  cmd="${cmd#"${cmd%%[![:space:]]*}"}"; cmd="${cmd%"${cmd##*[![:space:]]}"}"
  case "$name" in ''|'#'*) continue ;; esac
  case "$req" in
    required) N_REQUIRED=$((N_REQUIRED + 1)) ;;
    optional) ;;
    *)
      echo "gate $name: second field must be 'required' or 'optional' (got '$req')" >&2
      add MISCONF "$name=bad-line"; continue ;;
  esac

  # A gate that cannot run is not a pass: required ones make the run MISCONFIGURED.
  problem=""
  if [ -z "$cmd" ]; then
    problem="empty-command"
  elif is_stub "$cmd"; then
    problem="stub"
  else
    tool="$(harness_first_word "$cmd")"
    if [ -n "$tool" ] && ! command -v "$tool" >/dev/null 2>&1 && [ ! -x "$tool" ]; then
      problem="missing-tool:$tool"
    fi
  fi
  if [ -n "$problem" ]; then
    if [ "$req" = required ]; then
      printf 'MISCONFIGURED  %s (%s)\n' "$name" "$problem"
      add MISCONF "$name=$problem"
    else
      printf 'SKIP  %s (%s)\n' "$name" "$problem"
      add SKIPPED "$name"
    fi
    continue
  fi

  printf '\n=== gate: %s ===\n$ %s\n' "$name" "$cmd"
  harness_run_timeout "$TIMEOUT" "$cmd" </dev/null
  rc=$?
  if [ "$rc" -eq 0 ]; then
    printf 'PASS  %s\n' "$name"
    add PASSED "$name"
  elif [ "$rc" -eq 127 ] && [ "$req" = required ]; then
    # exit 127 is the shell's "command not found": the gate could not run
    printf 'MISCONFIGURED  %s (command not found, exit 127)\n' "$name"
    add MISCONF "$name=exit-127"
  elif [ "$rc" -eq 124 ]; then
    printf 'FAIL  %s (timed out after %ss)\n' "$name" "$TIMEOUT"
    add FAILED "$name"
  else
    printf 'FAIL  %s (exit %s)\n' "$name" "$rc"
    add FAILED "$name"
  fi
done < "$CONF"

if [ "$N_REQUIRED" -eq 0 ]; then
  add MISCONF "no-required-gates"
fi

SUMMARY="passed=${PASSED:-none} failed=${FAILED:-none} skipped=${SKIPPED:-none} misconfigured=${MISCONF:-none}"
printf '\n'
if [ -n "$MISCONF" ]; then
  verdict MISCONFIGURED "$SUMMARY" 2
elif [ -n "$FAILED" ]; then
  verdict FAIL "$SUMMARY" 1
fi

STATE=".harness/.state"
mkdir -p "$STATE" && harness_tree_hash > "$STATE/gates-passed.tmp" \
  && mv "$STATE/gates-passed.tmp" "$STATE/gates-passed" \
  && rm -f "$STATE/dirty-paths" \
  || echo "warning: could not write $STATE/gates-passed" >&2
verdict PASS "$SUMMARY" 0
