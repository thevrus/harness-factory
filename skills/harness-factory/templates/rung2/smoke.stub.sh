#!/usr/bin/env bash
# HARNESS_STUB: this smoke check has not been defined yet, so it fails.
# Managed by harness-factory. A stub must never pass: an unchecked core flow
# is not evidence of anything.
#
# To fill it in, set core_flow.smoke_command in .harness/config.json to one
# command that exercises the product's core flow end to end (for example:
# start the app, hit one real endpoint or run one real CLI path, check the
# output), then re-run the installer. Or replace this file by hand.

cat >&2 <<'MSG'
smoke.sh is a stub (HARNESS_STUB). Define the core-flow smoke check:
  1. Set "core_flow.smoke_command" in .harness/config.json.
  2. Re-run the harness-factory installer for rung 2.
The command should run the real core flow, not unit tests.
MSG
echo "HARNESS_SMOKE: MISCONFIGURED (stub: core_flow.smoke_command is empty)"
exit 2
