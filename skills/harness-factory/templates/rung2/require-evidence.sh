#!/usr/bin/env bash
# require-evidence.sh: stop hook. Keeps the agent from finishing while the
# tree has changed since the last passing gate run. Managed by harness-factory.
#
# Usage: require-evidence.sh [--agent ID]   (hook payload on stdin; default ID claude)
#
# Passes through when:
#   - the agent is already continuing because of a stop hook (loop guard);
#   - nothing was edited this session (no .harness/.state/dirty-paths);
#   - the tree hash equals .harness/.state/gates-passed (gates ran on it);
#   - every edited path is a non-code file (docs, text, images).
# Otherwise it blocks once and tells the agent to run .harness/bin/gates.sh.
#
# Fail-open on internal errors, by design. A stop hook that cannot read its
# payload cannot honor the loop guard, so blocking blind could trap a session
# until the host's consecutive-block cap. The agent host also ignores
# onFailure=block for stop events, so the registration does not set it. The
# hard enforcement is gates.sh itself, in CI and before merge; this hook is
# the in-session reminder that makes the agent run it.

set -u

AGENT="claude"
[ "${1:-}" = "--agent" ] && [ -n "${2:-}" ] && AGENT="$2"
INPUT="$(cat 2>/dev/null || true)"

# ---- adapter layer: the only agent-specific code in this file -------------
# reentry: exit 0 if the agent is already continuing due to a stop hook.
# block REASON: emit the agent's "do not stop yet" response.
#   claude   Stop; stop_hook_active; {"decision":"block","reason"} on stdout
#   codex    Stop; stop_hook_active; {"decision":"block","reason"} (reason
#            becomes the next prompt)
#   copilot  agentStop; stop_hook_active; {"decision":"block","reason"}; the
#            CLI ends the turn anyway after 8 consecutive blocks
#   gemini   AfterAgent; stop_hook_active; {"decision":"deny","reason"}
#            rejects the reply and retries with reason as the prompt
#   cursor   stop; loop_count (0 on the first stop) and status; reply
#            {"followup_message"} is only used when status is "completed"
#            and is capped by the hook's loop_limit
reentry() {
  case "$AGENT" in
    claude|codex|copilot|gemini)
      # payload field stop_hook_active (bool); grep is the fallback parser
      if command -v python3 >/dev/null 2>&1; then
        printf '%s' "$INPUT" | python3 -c 'import json,sys
try:
    sys.exit(0 if json.load(sys.stdin).get("stop_hook_active") is True else 1)
except Exception:
    sys.exit(0)'
      else
        printf '%s' "$INPUT" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'
      fi
      ;;
    cursor)
      # one follow-up per stop, like the others: pass once a follow-up ran,
      # and when the loop did not complete (aborted or error)
      printf '%s' "$INPUT" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    sys.exit(0 if (d.get("loop_count") or 0) > 0 or d.get("status") != "completed" else 1)
except Exception:
    sys.exit(0)' 2>/dev/null
      ;;
    *) echo "require-evidence: no adapter for agent '$AGENT'; passing" >&2; return 0 ;;
  esac
}
block() {
  local field="reason" decision="block"
  case "$AGENT" in
    claude|codex|copilot) ;;
    gemini) decision="deny" ;;
    cursor) decision="" field="followup_message" ;;
  esac
  python3 -c 'import json,sys
d, f, r = sys.argv[1], sys.argv[2], sys.argv[3]
out = {f: r}
if d:
    out["decision"] = d
print(json.dumps(out))' "$decision" "$field" "$1" 2>/dev/null \
    || printf '{"decision":"block","reason":"Run .harness/bin/gates.sh and quote its HARNESS_GATES line before finishing."}\n'
  exit 0
}
# ---------------------------------------------------------------------------

reentry && exit 0

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SELF_DIR/../bin/_lib.sh"
[ -f "$LIB" ] || exit 0
# shellcheck source=../bin/_lib.sh disable=SC1091
. "$LIB"

ROOT="$(harness_root)" || exit 0
cd "$ROOT" || exit 0
STATE=".harness/.state"
[ -s "$STATE/dirty-paths" ] || exit 0

CURRENT="$(harness_tree_hash 2>/dev/null)" || exit 0
if [ -f "$STATE/gates-passed" ] && [ "$CURRENT" = "$(cat "$STATE/gates-passed")" ]; then
  rm -f "$STATE/dirty-paths"
  exit 0
fi

# Cheap non-code filter on the paths the agent edited. Unknown ("?") counts as code.
code_paths=""
while IFS= read -r p; do
  [ -n "$p" ] || continue
  case "$p" in
    *.md|*.markdown|*.mdx|*.txt|*.rst|*.adoc|*.png|*.jpg|*.jpeg|*.gif|*.svg|*.webp|*.ico|*.pdf) ;;
    LICENSE*|CHANGELOG*|docs/*|.harness/.state/*) ;;
    *) code_paths="${code_paths}${code_paths:+, }$p" ;;
  esac
done < <(sort -u "$STATE/dirty-paths")
[ -n "$code_paths" ] || exit 0

block "Code changed since the last passing gate run (edited: ${code_paths:0:300}). Run .harness/bin/gates.sh, fix any failure, and quote its final HARNESS_GATES line as evidence before finishing. If gates report MISCONFIGURED, say so plainly instead of claiming the work is verified."
