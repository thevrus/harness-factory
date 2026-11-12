#!/usr/bin/env bash
# Record a real agent failure as a regression case under .harness/failures/.
# Usage: new-failure-case.sh <pr-or-issue-number> [signature-id]
# Writes task.md, base_sha, expected.md (for a human to fill) and signature.
# Needs gh (authenticated) and jq; fails closed without them.
set -euo pipefail

die() { echo "new-failure-case: $*" >&2; exit 2; }
command -v gh >/dev/null 2>&1 || die "MISCONFIGURED: gh not found"
command -v jq >/dev/null 2>&1 || die "MISCONFIGURED: jq not found"
[ $# -ge 1 ] || die "usage: new-failure-case.sh <pr-or-issue-number> [signature-id]"
n="$1"
sig="${2:-unclassified}"
case "$n" in ''|*[!0-9]*) die "number must be digits" ;; esac

root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "run inside the repository"
cd "$root"
sigfile=".harness/failure-signatures.yml"
if [ "$sig" != "unclassified" ] && ! grep -qE "^- id: ${sig}\$" "$sigfile" 2>/dev/null; then
  die "signature '$sig' is not in $sigfile"
fi

repo="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
item="$(gh api "repos/$repo/issues/$n")"
title="$(jq -r '.title' <<<"$item")"
slug="$(tr '[:upper:]' '[:lower:]' <<<"$title" | tr -cs 'a-z0-9' '-' | cut -c1-40 | sed 's/^-//; s/-$//')"
dir=".harness/failures/$(date -u +%Y-%m-%d)-$n-${slug:-case}"
[ -e "$dir" ] && die "$dir already exists"

if [ "$(jq -r '.pull_request != null' <<<"$item")" = "true" ]; then
  kind="pull request"
  base="$(gh api "repos/$repo/pulls/$n" --jq '.base.sha')"
else
  kind="issue"
  branch="$(gh api "repos/$repo" --jq '.default_branch')"
  base="$(gh api "repos/$repo/commits/$branch" --jq '.sha')"
fi

mkdir -p "$dir"
printf '%s\n' "$base" > "$dir/base_sha"
printf '%s\n' "$sig" > "$dir/signature"
{
  printf '# Task (from %s #%s)\n\n' "$kind" "$n"
  printf 'Source: %s\n\n' "$(jq -r '.html_url' <<<"$item")"
  printf 'The text below is a copy of untrusted issue or PR text, kept as data.\n\n'
  printf '## %s\n\n' "$title"
  jq -r '.body // "(empty body)"' <<<"$item" | sed 's/^/> /'
} > "$dir/task.md"
cat > "$dir/expected.md" <<'EOF'
# Expected outcome (fill in by hand)

- What the agent did wrong:
- What a correct change does:
- How to check it (test name, command, or the gate verdict that must appear):
- Golden fix, if one was merged (PR number or commit):
EOF
echo "created $dir (signature: $sig)"
[ "$sig" = "unclassified" ] && echo "note: pass a signature id from $sigfile so repeat-failure rate can count it" >&2
exit 0
