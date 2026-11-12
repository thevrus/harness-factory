#!/usr/bin/env bash
# mark-dirty.sh: after-edit hook. Records which file the agent just edited in
# .harness/.state/dirty-paths so the stop hook knows this session changed
# code. Managed by harness-factory. Registered per agent by its adapter.
#
# Usage: mark-dirty.sh [--agent ID]   (hook payload on stdin; default ID claude)
# Never blocks and never prints: a bookkeeping failure must not break edits.
# The stop hook still compares the real tree hash, so a missed mark only
# means one fewer reminder, not a false pass of the gates.

set -u

AGENT="claude"
[ "${1:-}" = "--agent" ] && [ -n "${2:-}" ] && AGENT="$2"

# ---- adapter layer: the only agent-specific code in this file -------------
# Prints the edited file path(s), one per line (absolute or repo-relative),
# from the payload; "?" when it cannot tell.
#   claude   tool_input.file_path (Edit, Write) or tool_input.notebook_path
#   codex    PostToolUse apply_patch: tool_input.command is the patch text;
#            paths come from its Add/Update/Delete File and Move to headers
#   cursor   afterFileEdit: top-level file_path
#   gemini   AfterTool write_file/replace: tool_input.file_path
#   copilot  postToolUse: toolArgs (object or JSON string); path key names
#            are not documented, so any key mentioning "path" or "file"
edited_paths() {
  python3 -c 'import json,sys
agent = sys.argv[1]
def patch(t):
    if not isinstance(t, str) or "*** Begin Patch" not in t:
        return []
    heads = ("*** Add File:", "*** Delete File:", "*** Update File:", "*** Move to:")
    return [l.strip()[len(h):].strip() for l in t.splitlines() for h in heads if l.strip().startswith(h)]
def pathargs(d):
    out = []
    for k, v in d.items():
        if "path" in k.lower() or "file" in k.lower():
            out += [i for i in (v if isinstance(v, list) else [v]) if isinstance(i, str) and i and "\n" not in i]
    return out
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
ti = d.get("toolArgs", d.get("tool_input")) or {}
if isinstance(ti, str):
    try:
        ti = json.loads(ti)
    except Exception:
        ti = {"command": ti}
if not isinstance(ti, dict):
    ti = {}
if agent == "claude":
    out = [ti.get("file_path") or ti.get("notebook_path")]
elif agent == "codex":
    out = patch(ti.get("command"))
elif agent == "cursor":
    out = [d.get("file_path")]
elif agent == "gemini":
    out = [ti.get("file_path")]
elif agent == "copilot":
    out = patch(ti.get("command")) or patch(ti.get("input")) or pathargs(ti)
else:
    out = []
out = [p for p in out if p]
print("\n".join(out) if out else "?")' "$AGENT" 2>/dev/null || echo "?"
}
# ---------------------------------------------------------------------------

root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
mkdir -p "$root/.harness/.state" 2>/dev/null || exit 0
while IFS= read -r path; do
  [ -n "$path" ] || continue
  rel="$(python3 -c 'import os,sys
p, r = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])
print(os.path.relpath(p, r) if sys.argv[1] != "?" else "?")' "$path" "$root" 2>/dev/null || echo "?")"
  case "$rel" in ../*) continue ;; esac
  printf '%s\n' "$rel" >> "$root/.harness/.state/dirty-paths"
done < <(edited_paths)
exit 0
