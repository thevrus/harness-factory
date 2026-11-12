# shellcheck shell=bash
# Shared helpers for .harness/bin and .harness/hooks. Sourced, not executed.
# Managed by harness-factory.

# harness_root: print the repo root (git toplevel), or fail.
harness_root() {
  git rev-parse --show-toplevel 2>/dev/null
}

# harness_tree_hash: identify the exact working state so a later check can
# tell whether gates ran on it. HEAD plus a hash of status, the full diff
# against HEAD (staged and unstaged) and untracked file contents. Local state
# under .harness/.state is excluded so writing a stamp does not change it.
harness_tree_hash() {
  local head ex=':(exclude).harness/.state'
  head="$(git rev-parse --verify -q HEAD 2>/dev/null || echo no-head)"
  {
    git status --porcelain=v1 --untracked-files=all -- . "$ex"
    if [ "$head" = "no-head" ]; then
      git diff --binary --cached -- . "$ex"
      git diff --binary -- . "$ex"
    else
      git diff --binary HEAD -- . "$ex"
    fi
    git ls-files -o --exclude-standard -z -- . "$ex" | _harness_untracked_hashes
  } | { printf '%s ' "$head"; git hash-object --stdin; }
}

# _harness_untracked_hashes: NUL-separated paths on stdin; prints
# "<blob hash> <path>" for each regular file, in input order. One
# `git hash-object --stdin-paths` for the lot; names it cannot carry (a
# newline or CR, a leading quote), an unreadable file, or any failure fall
# back to one hash-object per file, which gives the same lines.
_harness_untracked_hashes() {
  local f n=0 slow=0 hashes h i
  local -a files
  while IFS= read -r -d '' f; do
    [ -f "$f" ] || continue
    case "$f" in
      *$'\n'*|*$'\r'*|'"'*) slow=1 ;;
    esac
    [ -r "$f" ] || slow=1
    files[n]="$f"
    n=$((n + 1))
  done
  [ "$n" -eq 0 ] && return 0
  if [ "$slow" -eq 0 ]; then
    if hashes="$(printf '%s\n' "${files[@]}" | git hash-object --stdin-paths 2>/dev/null)" &&
      [ "$(printf '%s\n' "$hashes" | wc -l | tr -d ' ')" -eq "$n" ]; then
      i=0
      while IFS= read -r h; do
        printf '%s %s\n' "$h" "${files[i]}"
        i=$((i + 1))
      done <<<"$hashes"
      return 0
    fi
  fi
  for f in "${files[@]}"; do
    printf '%s %s\n' "$(git hash-object -- "$f")" "$f"
  done
}

# harness_run_timeout SECONDS COMMAND_STRING
# Runs COMMAND_STRING with bash -c in its own process group and kills the
# whole group on timeout (coreutils timeout is not on every machine).
# Returns the command's status, or 124 on timeout.
harness_run_timeout() {
  local secs="$1" cmd="$2" pid watcher rc flag s
  flag="$(mktemp "${TMPDIR:-/tmp}/harness-timeout.XXXXXX")"
  rm -f "$flag"
  set -m
  bash -c "$cmd" &
  pid=$!
  set +m
  (
    # kill our own sleep when the command finishes first, so nothing lingers
    trap 'kill "$s" 2>/dev/null; exit 0' TERM
    sleep "$secs" &
    s=$!
    wait "$s"
    if kill -0 "$pid" 2>/dev/null; then
      : > "$flag"
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
      for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || exit 0
        sleep 1
      done
      kill -KILL -- "-$pid" 2>/dev/null || true
    fi
  ) &
  watcher=$!
  wait "$pid"
  rc=$?
  kill "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null
  if [ -e "$flag" ]; then
    rm -f "$flag"
    return 124
  fi
  return "$rc"
}

# harness_first_word COMMAND_STRING: the executable a command will run,
# skipping leading VAR=value assignments. Empty if it cannot tell.
harness_first_word() {
  local w
  set -f
  # word splitting is the point here; globbing is off
  # shellcheck disable=SC2086
  set -- $1
  set +f
  for w in "$@"; do
    case "$w" in
      [A-Za-z_]*=*) continue ;;
      *) printf '%s' "$w"; return 0 ;;
    esac
  done
}
