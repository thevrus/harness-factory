#!/usr/bin/env bash
# smoke.sh: run the core-flow smoke check with a timeout.
# Managed by harness-factory; rendered from core_flow.smoke_command in
# .harness/config.json. Change the config and re-run the installer, or edit
# this file (the installer then reports it as customized, not overwritten).
#
# The core flow it checks is described in .harness/config.json (core_flow).
# Last line: HARNESS_SMOKE: PASS|FAIL|MISCONFIGURED (<detail>)
# Exit: 0 PASS, 1 FAIL, 2 MISCONFIGURED (the command could not run).
# Env: HARNESS_SMOKE_TIMEOUT seconds (default 300).

set -uo pipefail

SMOKE_CMD={{core_flow.smoke_command|sh}}
TIMEOUT="${HARNESS_SMOKE_TIMEOUT:-300}"

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh disable=SC1091
. "$SELF_DIR/_lib.sh"

ROOT="$(harness_root)" || { echo "HARNESS_SMOKE: MISCONFIGURED (not inside a git repository)"; exit 2; }
cd "$ROOT" || exit 2

printf '$ %s\n' "$SMOKE_CMD"
harness_run_timeout "$TIMEOUT" "$SMOKE_CMD" </dev/null
rc=$?
case "$rc" in
  0) echo "HARNESS_SMOKE: PASS (exit 0)"; exit 0 ;;
  124) echo "HARNESS_SMOKE: FAIL (timed out after ${TIMEOUT}s)"; exit 1 ;;
  127) echo "HARNESS_SMOKE: MISCONFIGURED (command not found, exit 127)"; exit 2 ;;
  *) echo "HARNESS_SMOKE: FAIL (exit $rc)"; exit 1 ;;
esac
