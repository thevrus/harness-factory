#!/usr/bin/env python3
"""Guarded-rung logic shared by the hooks and the diff guard (stdlib only).

Subcommands:
  hook-path  --format F   PreToolUse-style file-edit guard; hook JSON on stdin
  hook-bash  --format F   shell-command guard; hook JSON on stdin
  check-path PATH...      print which paths are protected (debugging aid)
  diff       [--base REF] [--head REF] [--allow-protected]
  codeowner-approval      decide whether a CODEOWNER approved the PR head (CI)

File-edit verdicts: allow; approve (a protected path: the edit needs a human's
approval, which each adapter maps onto its agent's confirmation prompt); block
(HARD_BLOCKED paths, which no session may edit). Any failure to decide blocks,
because the hooks are registered to fail closed anyway and a guard that cannot
read its policy must not wave changes through.

Agent-specific hook formats live in the ADAPTERS table near the bottom. Each
adapter is one parse function (raw stdin -> neutral request) and one respond
function (verdict -> exit code / output). Add a new agent there.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys

# Substituted at install time; the config file wins at run time.
DEFAULT_BRANCH_FALLBACK = "{{default_branch}}"

# Always protected, even if someone trims .harness/protected-paths.txt.
# Applied after the file's own lines so a "!pattern" there cannot undo them.
ALWAYS_PROTECTED = [
    ".harness/**",
    ".harness/acceptance/**",
    ".github/workflows/**",
    ".claude/**",
    ".codex/**",
    ".cursor/**",
    ".gemini/**",
    ".agents/**",
    ".github/hooks/**",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".mcp.json",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
]

LOCKFILES = [
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lock", "bun.lockb", "Cargo.lock", "Gemfile.lock", "poetry.lock",
    "uv.lock", "Pipfile.lock", "pdm.lock", "composer.lock", "go.sum",
    "mix.lock", "pubspec.lock", "Podfile.lock", "packages.lock.json",
    "flake.lock", "gradle.lockfile", ".terraform.lock.hcl",
]

# Excluded from the diff-size count (still subject to the protected check).
# Only names that toolchains emit and nobody hand-writes stay here. A pattern
# anyone can pick for a hand-written file ("*.generated.*", "*.snap",
# "__snapshots__/") would let a change dodge the limit by naming; repos that
# really generate such files list them in .harness/generated-paths.txt, which
# is read from the base branch so a PR cannot add to it.
GENERATED_DEFAULTS = [
    "*.map",
    "*.pb.go", "*_pb2.py", "*_pb2_grpc.py",
]

# Never editable by the agent, even with a human at the prompt: the agent is
# judged on these scenarios, so it must not be able to rewrite them.
HARD_BLOCKED = [".harness/acceptance/**"]

PROTECTED_FILE = ".harness/protected-paths.txt"
GENERATED_FILE = ".harness/generated-paths.txt"
CONFIG_FILE = ".harness/config.json"
APPROVAL_ENV = "HARNESS_PROTECTED_APPROVED"
CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32", "cygwin")


class Misconfigured(Exception):
    """The guard cannot evaluate its policy; callers fail closed."""


# --------------------------------------------------------------------------
# gitignore-style matching
# --------------------------------------------------------------------------

def _translate(pat):
    """Translate a gitignore glob body into a regex fragment."""
    out, i, n = [], 0, len(pat)
    while i < n:
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("/**", i) and i + 3 == n:
            out.append("/.*")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = pat[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
            continue
        elif c == "[":
            j = pat.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1:j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class Rule(object):
    def __init__(self, text, negate, dir_only, regex):
        self.text = text
        self.negate = negate
        self.dir_only = dir_only
        self.regex = regex


def compile_rule(line):
    s = line.rstrip("\r\n")
    s = re.sub(r"(?<!\\)\s+$", "", s)
    if not s or s.startswith("#"):
        return None
    negate = s.startswith("!")
    if negate:
        s = s[1:]
    if s[:2] in ("\\#", "\\!"):
        s = s[1:]
    dir_only = s.endswith("/")
    s = s.rstrip("/")
    if not s:
        return None
    anchored = "/" in s
    body = _translate(s.lstrip("/"))
    prefix = "" if anchored else "(?:.*/)?"
    regex = re.compile(prefix + body + r"\Z", re.IGNORECASE | re.DOTALL)
    return Rule(line.strip(), negate, dir_only, regex)


def compile_rules(lines):
    rules = []
    for line in lines:
        r = compile_rule(line)
        if r is not None:
            rules.append(r)
    return rules


def match(rules, relpath):
    """Return the rule text that decides relpath is matched, else None.

    A pattern matches a path when it matches the path itself or any parent
    directory (gitignore: a matched directory covers everything inside it).
    The last matching rule wins, so "!pattern" can re-include.
    """
    parts = [p for p in relpath.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    candidates = []
    for k in range(1, len(parts) + 1):
        candidates.append(("/".join(parts[:k]), k < len(parts)))
    decided = None
    for rule in rules:
        hit = any(rule.regex.match(c) for c, is_dir in candidates
                  if is_dir or not rule.dir_only)
        if hit:
            decided = None if rule.negate else rule.text
    return decided


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def run_git(args, cwd=None, check=True):
    if shutil.which("git") is None:
        raise Misconfigured("git is not installed")
    p = subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise Misconfigured("git %s failed: %s" % (
            " ".join(args), p.stderr.decode("utf-8", "replace").strip()))
    return p


_TOPLEVEL = {}


def git_toplevel(path):
    """Memoized per process on path: one hook call asks for the same
    directory several times."""
    if path not in _TOPLEVEL:
        _TOPLEVEL[path] = _git_toplevel(path)
    return _TOPLEVEL[path]


def _git_toplevel(path):
    d = path
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    if not d or shutil.which("git") is None:
        return None
    p = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace").strip() or None


def git_show(ref, path, cwd):
    p = run_git(["show", "%s:%s" % (ref, path)], cwd=cwd, check=False)
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace")


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def relative_inside(path, root):
    """path relative to root if path is inside root, else None."""
    a, b = os.path.normpath(path), os.path.normpath(root)
    if CASE_INSENSITIVE_FS:
        a_cmp, b_cmp = a.lower(), b.lower()
    else:
        a_cmp, b_cmp = a, b
    if a_cmp == b_cmp:
        return ""
    if not a_cmp.startswith(b_cmp.rstrip(os.sep) + os.sep):
        return None
    return a[len(b.rstrip(os.sep)) + 1:].replace(os.sep, "/")


def protected_rules_for_root(root, required):
    text = read_text(os.path.join(root, PROTECTED_FILE))
    if text is None:
        if required:
            raise Misconfigured("%s is missing or unreadable in %s" % (PROTECTED_FILE, root))
        text = ""
    return compile_rules(text.splitlines() + ALWAYS_PROTECTED + LOCKFILES)


def load_config(root, base_ref=None):
    text = None
    if base_ref:
        text = git_show(base_ref, CONFIG_FILE, root)
    if text is None:
        text = read_text(os.path.join(root, CONFIG_FILE))
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        raise Misconfigured("%s is not valid JSON" % CONFIG_FILE)
    return data if isinstance(data, dict) else None


def protected_branches(root):
    names = {"main", "master"}
    fallback = DEFAULT_BRANCH_FALLBACK
    if "{" * 2 not in fallback and fallback.strip():
        names.add(fallback.strip())
    try:
        cfg = load_config(root) if root else None
    except Misconfigured:
        cfg = None
    if cfg and isinstance(cfg.get("default_branch"), str) and cfg["default_branch"].strip():
        names.add(cfg["default_branch"].strip())
    return names


# --------------------------------------------------------------------------
# file-edit guard
# --------------------------------------------------------------------------

def check_paths(paths, cwd, project_dir):
    """Return (verdict, reason), verdict in allow/approve/block.

    Raises Misconfigured when undecidable. The strictest verdict across all
    target paths and their resolved forms wins.
    """
    if not paths:
        raise Misconfigured("the tool call names no target path")
    project_real = os.path.realpath(project_dir) if project_dir else None
    base_roots = set()
    if project_dir:
        base_roots.update((project_dir, project_real))
    if cwd:
        top = git_toplevel(cwd)
        if top:
            base_roots.update((top, os.path.realpath(top)))
    hard = compile_rules(HARD_BLOCKED)
    verdict, reason = "allow", ""
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            raise Misconfigured("target path is empty or not a string")
        base = cwd or project_dir or os.getcwd()
        full = raw if os.path.isabs(raw) else os.path.join(base, raw)
        lexical = os.path.normpath(full)   # collapses "..", keeps symlinks
        real = os.path.realpath(full)      # follows symlinks
        roots = set(base_roots)
        for form in (lexical, real):
            top = git_toplevel(form)
            if top:
                roots.update((top, os.path.realpath(top)))
        if not roots:
            raise Misconfigured("cannot locate the repository root for %s" % raw)
        for form in (lexical, real):
            hit = _judge(form, roots, project_real)
            if hit:
                rel, pattern = hit
                shown = rel if form == lexical else "%s (resolves to %s)" % (raw, rel)
                hard_pat = match(hard, rel)
                if hard_pat:
                    return "block", "%s is never agent-editable (pattern %r)" % (shown, hard_pat)
                if verdict == "allow":
                    verdict, reason = "approve", "%s is a protected path (pattern %r in %s)" % (
                        shown, pattern, PROTECTED_FILE)
    return verdict, reason


def _judge(form, roots, project_real):
    """Judge one absolute path against the repo roots that contain it.

    The innermost root decides, so a linked git worktree (for example one an
    agent creates under a protected directory) is judged by its own checkout.
    An outer root still applies when the inner root is an embedded repository
    rather than a linked worktree, so `git init` inside a protected directory
    does not open a hole.
    """
    containing = []
    seen = set()
    for r in roots:
        rel = relative_inside(form, r)
        key = os.path.realpath(r)
        if rel and key not in seen:
            seen.add(key)
            containing.append((len(os.path.normpath(r)), r, rel))
    containing.sort(reverse=True)
    for idx, (_, root, rel) in enumerate(containing):
        if idx > 0:
            inner = containing[idx - 1][1]
            if os.path.isfile(os.path.join(inner, ".git")):
                break  # inner root is a linked worktree: its own policy decides
        required = project_real is not None and os.path.realpath(root) == project_real
        pattern = match(protected_rules_for_root(root, required), rel)
        if pattern:
            return rel, pattern
    return None


# --------------------------------------------------------------------------
# shell-command guard (best effort; see guard-bash.sh header)
# --------------------------------------------------------------------------

SEPARATOR_CHARS = set(";&|()\n")
WRAPPERS = {"sudo", "nohup", "nice", "time", "command", "builtin", "noglob",
            "stdbuf", "env", "xargs", "exec", "doas", "caffeinate"}
WRAPPERS_WITH_ARG = {"timeout"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "pwsh", "powershell"}
INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "deno", "bun", "php"}


def is_interpreter(name):
    return name in INTERPRETERS or re.match(r"^python3\.\d+$", name)


GIT_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                     "--exec-path", "--config-env", "--super-prefix"}
PUSH_OPTS_WITH_ARG = {"-o", "--push-option", "--repo", "--receive-pack",
                      "--exec", "--signed"}
TEXT_PATTERNS = [
    (re.compile(r"mergePullRequest|enablePullRequestAutoMerge|mergeBranch", re.I),
     "GitHub GraphQL merge mutation"),
    (re.compile(r"/pulls/[^/\s]+/merge\b"), "GitHub REST pull request merge"),
    (re.compile(r"api\.github\.com/repos/[^\s]*/merges\b"), "GitHub REST branch merge"),
]


def tokenize(text):
    text = text.replace("`", "\n")
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def split_commands(tokens, writes=None):
    """Split tokens into simple commands. Output-redirection targets (">",
    ">>", ">|", "&>", "<>", ">&file") are dropped from the argv and, when
    `writes` is a list, appended to it."""
    cmds, cur, skip_next, capture = [], [], False, False
    for tok in tokens:
        if skip_next:
            skip_next = False
            if capture and writes is not None and not re.match(r"^\d*-?$", tok):
                writes.append(tok)  # ">&1" duplicates a descriptor, not a file
            continue
        if tok and set(tok) <= set(";&|()<>\n"):
            if (">" in tok or "<" in tok) and not set(tok) & set("()"):
                # "<(" / ">(" is process substitution: a command follows, not a file
                skip_next, capture = True, ">" in tok
                if cur and cur[-1].isdigit():
                    cur.pop()  # "2>&1": the 2 is a descriptor, not an argument
                if set(tok) & SEPARATOR_CHARS - {"&", "|"}:
                    if cur:
                        cmds.append(cur)
                    cur = []
            elif set(tok) & SEPARATOR_CHARS:
                if cur:
                    cmds.append(cur)
                cur = []
            continue
        if tok == "$":
            continue  # the "$" of "$(" ; the command inside is split out
        cur.append(tok)
    if cur:
        cmds.append(cur)
    return cmds


# --- protected paths reached through the shell ----------------------------
#
# Edit/Write to protected paths needs a human (HARD_BLOCKED paths are blocked
# outright); the shell would otherwise be a side door (rm -rf ..., echo > ...,
# sed -i ...). Two tiers share one resolver:
#   tier 1  HARD_BLOCKED                -> "block"
#   tier 2  protected_rules_for_root()  -> "approve" (same verdict an edit tool
#           gets; adapters without "ask" turn it into a block)
# A command hits a tier when a write-redirect target, or an argument of a
# command that is not on the read-only list, resolves to a path of that tier
# or to a directory that contains one (other than the repository root).
# Unresolvable arguments ($VAR, globs, ~) that mention a tier's name hit it.
# Tier 2 does not judge the program being run (argv[0], or the script given to
# a shell or interpreter): running .harness/bin/gates.sh is not editing it.

READ_ONLY = {"cat", "ls", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc",
             "diff", "cmp", "file", "stat", "test", "[", "realpath", "readlink",
             "du", "tree", "md5sum", "sha1sum", "sha256sum", "shasum", "md5",
             "basename", "dirname", "nl", "od", "hexdump", "pwd", "true", "cd",
             "pushd", "find", "git", "echo", "printf"}
FIND_WRITERS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
                "-fprint0", "-fprintf", "-fls"}
GIT_READ_ONLY = {"diff", "log", "show", "status", "blame", "ls-files", "ls-tree",
                 "cat-file", "grep", "rev-parse", "shortlog", "describe"}
# Tier 2 only: these touch the index or history, not the working tree file.
GIT_INDEX_ONLY = {"add", "stage", "commit"}
STDIN_RUNNERS = {"xargs", "parallel", "source", "."} | SHELLS
# Options whose value is prose, not a path (dropped before path checks).
GIT_COMMIT_TEXT_OPTS = {"-m", "--message", "-F", "--file", "-C", "-c",
                        "--reuse-message", "--reedit-message", "--template", "-t"}
GH_TEXT_OPTS = {"--title", "-t", "--body", "-b"}


def _literal_dirs(patterns):
    """Literal paths that patterns cover, lower-cased ("a/b/**" -> "a/b")."""
    out = []
    for p in patterns:
        p = p.strip()
        if not p or p[0] in "#!":
            continue
        d = re.sub(r"/\*\*$", "", p).strip("/")
        if d and not any(c in d for c in "*?[\\"):
            out.append(d.lower())
    return out


def _hard_dirs():
    return _literal_dirs(HARD_BLOCKED)


class Tier(object):
    """One class of shell-protected paths: rules per root, literal names."""

    def __init__(self, verdict, label, rules_for, names):
        self.verdict = verdict      # "block" or "approve"
        self.label = label          # "never agent-editable" / "a protected path"
        self.rules_for = rules_for  # root -> compiled rules
        self.names = names          # literal lower-cased paths (dirs or files)


def hard_tier():
    hard = compile_rules(HARD_BLOCKED)
    return Tier("block", "never agent-editable", lambda root: hard, _hard_dirs())


def protected_tier(roots):
    cache = {}
    names = set()
    for r in roots:
        rules = protected_rules_for_root(r, False)
        cache[os.path.realpath(r)] = rules
        names.update(_literal_dirs(rule.text for rule in rules if not rule.negate))

    def rules_for(root):
        key = os.path.realpath(root)
        if key not in cache:
            cache[key] = protected_rules_for_root(root, False)
        return cache[key]
    return Tier("approve", "a protected path", rules_for, sorted(names))


def is_read_only(argv, tier2=False):
    name = os.path.basename(argv[0]).lower()
    if name not in READ_ONLY:
        return False
    if name == "find":
        return not any(t in FIND_WRITERS for t in argv[1:])
    if name == "rg":
        return not any(t.startswith("--pre") for t in argv[1:])
    if name == "git":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            if argv[i] == "-C":
                i += 2
            elif argv[i] == "--no-pager":
                i += 1
            else:
                return False  # -c, --git-dir, ...: not plainly read-only
        allowed = GIT_READ_ONLY | (GIT_INDEX_ONLY if tier2 else set())
        if i >= len(argv) or argv[i] not in allowed:
            return False
        return not any(t.startswith("--output") or t in ("-O", "--open-files-in-pager")
                       or t.startswith("--ext-diff") or t.startswith("--textconv")
                       for t in argv[i + 1:])
    return True


def drop_text_args(argv):
    """argv without the values that are prose rather than paths: git commit
    -m/-F/--message, gh --title/--body. echo/printf are read-only instead."""
    if not argv:
        return argv
    name = os.path.basename(argv[0]).lower()
    if name == "git":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if (argv[i] in GIT_OPTS_WITH_ARG and "=" not in argv[i]) else 1
        if i >= len(argv) or argv[i] != "commit":
            return argv
        out, j, rest = argv[:i + 1], i + 1, argv
        while j < len(rest):
            t = rest[j]
            opt = t.split("=", 1)[0]
            if t == "--":
                out.extend(rest[j:])
                break
            if t.startswith("--") and "=" in t and opt in GIT_COMMIT_TEXT_OPTS:
                j += 1
            elif t in GIT_COMMIT_TEXT_OPTS or re.match(r"^-[a-zA-Z]*[mF]$", t):
                j += 2  # "-am MSG": the value follows the cluster
            elif re.match(r"^-[a-zA-Z]*m.", t) and not t.startswith("--"):
                j += 1  # "-mMSG"
            else:
                out.append(t)
                j += 1
        return out
    if name == "gh":
        out, j = [argv[0]], 1
        while j < len(argv):
            t = argv[j]
            if t.split("=", 1)[0] in GH_TEXT_OPTS:
                j += 1 if "=" in t else 2
                continue
            out.append(t)
            j += 1
        return out
    return argv


def _glob_could_match(pat, path):
    """Shell-style: per path segment, and a leading "." must be literal (no
    dotglob). "**" is treated as one segment, as bash does by default."""
    from fnmatch import fnmatchcase
    ps, xs = pat.split("/"), path.split("/")
    if len(ps) != len(xs):
        return False
    for p, x in zip(ps, xs):
        if x.startswith(".") and not p.startswith("."):
            return False
        if not fnmatchcase(x, p):
            return False
    return True


def hard_hit(token, cwd, roots, tier=None):
    """Reason string when token names (or may name) a path of the tier
    (default: HARD_BLOCKED)."""
    tier = tier or hard_tier()
    names = tier.names
    candidates = [token]
    if "=" in token:
        candidates.append(token.split("=", 1)[1])  # --out=PATH
    for cand in candidates:
        if not cand:
            continue
        low = cand.lower()
        if re.search(r"[$`{]", cand):
            if tier.verdict == "block":
                short = [d.rsplit("/", 1)[-1] for d in names]
                hit = any(n in low for n in short)
            else:
                hit = any(_mentions(low, n) for n in names)
            if hit:
                return "argument %r may expand to %s" % (token, tier.label)
            continue  # cannot resolve; judged by name only
        cand = os.path.expanduser(cand)
        full = cand if os.path.isabs(cand) else os.path.join(cwd or os.getcwd(), cand)
        forms = [os.path.normpath(full), os.path.realpath(full)]
        if re.search(r"[*?\[]", cand):
            # A glob: what it expands to now, and whether it could match a
            # protected path or one of its parents.
            import glob
            forms += glob.glob(full)
            covered = set()
            for d in names:
                parts = d.split("/")
                covered.update("/".join(parts[:k]) for k in range(1, len(parts) + 1))
            for root in roots:
                rel = relative_inside(os.path.normpath(full), root)
                if rel and any(_glob_could_match(rel.lower(), c) for c in covered):
                    return "glob %r can match %s" % (token, tier.label)
        for form in forms:
            for root in roots:
                rel = relative_inside(form, root)
                if not rel:
                    continue  # outside this root, or the root itself
                rl = rel.lower().rstrip("/")
                pattern = match(tier.rules_for(root), rel)
                if pattern or any(d == rl or d.startswith(rl + "/") for d in names):
                    return "%s reaches %s, which is %s (%s)" % (
                        token, rel, tier.label, pattern or "contains one")
    return None


def _mentions(text, name):
    """name appears in text as a path start (not inside ~/.claude or x.harness)."""
    return re.search(r"(?:^|[\s'\"(=,:;]|\./)" + re.escape(name) + r"(?![\w.-])", text) is not None


def _judged_args(argv, tier):
    """Arguments the tier judges. Tier 2 skips the program being run and the
    script a shell or interpreter runs, and the sources of a plain cp."""
    if tier.verdict == "block":
        return list(argv)
    name = os.path.basename(argv[0]).lower()
    args = argv[1:]
    if name in SHELLS or is_interpreter(name) \
            or name in ("source", "."):
        for k, t in enumerate(args):
            if not t.startswith("-"):
                return args[:k] + args[k + 1:]
            if re.match(r"^-[A-Za-z]*[ce]$", t) or t in ("-Command", "-command", "--eval"):
                return args  # inline code: no script file
        return args
    if name == "cp" and not any(t in ("-t", "--target-directory") or
                                t.startswith("--target-directory=") for t in args):
        pos = [k for k, t in enumerate(args) if not t.startswith("-")]
        if len(pos) > 1:
            return [t for k, t in enumerate(args) if k not in pos[:-1]]
    return args


def check_hard_paths(cmds, writes, cwd, roots, tier=None):
    """Reason when a shell write reaches a path of the tier (default
    HARD_BLOCKED). cmds are raw argv lists (before wrapper stripping); writes
    are redirect targets."""
    tier = tier or hard_tier()
    tier2 = tier.verdict != "block"
    for t in writes:
        why = hard_hit(t, cwd, roots, tier)
        if why:
            return "shell redirect: " + why
    tainted = False
    lockfiles = {l.lower() for l in LOCKFILES}
    for raw in cmds:
        argv = drop_text_args(strip_prefix(raw))
        if not argv:
            continue
        name = os.path.basename(argv[0]).lower()
        if name in ("cd", "pushd") and len(argv) > 1 and not argv[1].startswith("-"):
            target = argv[1]
            if not re.search(r"[$*?\[{`]", target):
                target = os.path.expanduser(target)
                cwd = target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target)
        read_only = is_read_only(argv, tier2)
        judged = _judged_args(argv, tier)
        hits = [h for h in (hard_hit(t, cwd, roots, tier) for t in judged) if h]
        if not read_only and not hits and name not in SHELLS and name != "eval":
            # (sh -c / eval text is re-checked as commands by the caller)
            # A path inside a larger argument: sed scripts, inline code.
            interp = is_interpreter(name)
            if not tier2:
                text = " ".join(argv[1:]).lower()
                for d in tier.names:
                    if d in text or (interp and d.rsplit("/", 1)[-1] in text):
                        hits.append("an argument mentions %s, which is %s" % (d, tier.label))
                        break
            else:
                code = [t.lower() for t in judged if re.search(r"[\s'\"(),;=]", t)]
                for d in tier.names:
                    if d not in lockfiles and any(_mentions(c, d) for c in code):
                        hits.append("an argument mentions %s, which is %s" % (d, tier.label))
                        break
        if hits and not read_only:
            return "shell command %r: %s" % (name, hits[0])
        if hits and not tier2:
            tainted = True
        runs_stdin = any(os.path.basename(t).lower() in STDIN_RUNNERS for t in raw[:len(raw) - len(argv) + 1])
        if tainted and runs_stdin:
            return "a never-agent-editable path is piped into %r" % name
    return None


HEREDOC_RX = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.-]+))")


def strip_heredocs(text):
    """Drop here-document bodies (they are data, not commands). The "<<" line
    stays, so its redirect target is still judged."""
    lines = text.split("\n")
    out, pending = [], []
    for line in lines:
        if pending:
            dash, delim = pending[0]
            cmp = line.lstrip("\t") if dash else line
            if cmp.strip() == delim:
                pending.pop(0)
            continue
        out.append(line)
        for m in HEREDOC_RX.finditer(line):
            if line[m.start() + 2:m.start() + 3] == "<" or line[m.start() - 1:m.start()] == "<":
                continue  # "<<<" is a here-string, no body
            delim = next(g for g in m.groups()[1:] if g is not None)
            pending.append((m.group(1) == "-", delim))
    return "\n".join(out)


def strip_prefix(argv):
    i = 0
    while i < len(argv):
        t = argv[i]
        base = os.path.basename(t)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
        elif base in WRAPPERS:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
        elif base in WRAPPERS_WITH_ARG:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            i += 1  # the duration
        else:
            break
    return argv[i:]


def branch_of_ref(ref):
    r = ref.lstrip("+")
    for pre in ("refs/heads/", "heads/"):
        if r.startswith(pre):
            r = r[len(pre):]
    return r


def current_branch(cwd):
    if not cwd or shutil.which("git") is None:
        return None
    p = subprocess.run(["git", "-C", cwd, "symbolic-ref", "--short", "-q", "HEAD"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace").strip() or None


def _local_branch_exists(gitdir, name):
    if not gitdir or shutil.which("git") is None:
        return False
    return subprocess.run(["git", "-C", gitdir, "rev-parse", "--verify", "-q", "refs/heads/" + name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def track_switch(sub, rest, gitdir, branches, protected=()):
    """Record in branches[gitdir] the branch a git checkout/switch moves to, so a
    later bare git push in the same command line is judged on that branch."""
    key = os.path.realpath(gitdir or ".")
    if "--" in rest or any(t.startswith("--pathspec-from-file") for t in rest):
        return  # checkout of paths: the branch does not change
    new, positionals, track, j = None, [], False, 0
    while j < len(rest):
        t = rest[j]
        opt, eq, val = t.partition("=")
        if opt in ("-b", "-B", "-c", "-C", "--orphan", "--create", "--force-create"):
            if eq:
                new = val
            elif j + 1 < len(rest):
                new = rest[j + 1]
                j += 1
        elif re.match(r"^-[A-Za-z]*[bBcC]$", t) and not t.startswith("--") and j + 1 < len(rest):
            new = rest[j + 1]  # e.g. "-fb name"
            j += 1
        elif t == "-" or t == "@{-1}":
            branches.pop(key, None)  # previous branch: fall back to reading HEAD
            return
        elif t in ("-d", "--detach"):
            branches[key] = "HEAD"
            return
        elif opt in ("-t", "--track"):
            track = True
        elif not t.startswith("-"):
            positionals.append(t)
        j += 1
    if new is None and track and positionals and "/" in positionals[0]:
        new = positionals[0].split("/", 1)[1]  # git checkout -t origin/main -> main
    if new is not None:
        branches[key] = branch_of_ref(new)
        return
    if not positionals:
        return
    target = positionals[0]
    if (sub == "switch" or branch_of_ref(target).lower() in {b.lower() for b in protected}
          or _local_branch_exists(gitdir, branch_of_ref(target))):
        # git checkout <protected> switches even without a local branch (it
        # creates one from origin/<protected>); other names may be paths
        branches[key] = branch_of_ref(target)


def check_git(argv, cwd, protected, branches=None):
    i, gitdir = 1, cwd
    while i < len(argv) and argv[i].startswith("-"):
        opt = argv[i].split("=", 1)[0]
        if opt == "-c" and i + 1 < len(argv) and re.search(r"push|alias|url\.", argv[i + 1], re.I):
            return "git -c %s can redirect a push; run git push without it" % argv[i + 1]
        if opt == "-C" and i + 1 < len(argv):
            gitdir = os.path.join(cwd or "", argv[i + 1])
        if opt in GIT_OPTS_WITH_ARG and "=" not in argv[i]:
            i += 2
        else:
            i += 1
    if i >= len(argv):
        return None
    sub, rest = argv[i], argv[i + 1:]
    if sub == "send-pack":
        return "git send-pack bypasses push policy"
    if sub in ("checkout", "switch") and branches is not None:
        track_switch(sub, argv[i + 1:], gitdir, branches, protected)
        return None
    if sub != "push":
        return None
    switched = (branches or {}).get(os.path.realpath(gitdir or "."))

    def branch_now():
        return switched or current_branch(gitdir)

    lowered = {b.lower() for b in protected}
    positionals, has_repo = [], False
    j = 0
    while j < len(rest):
        t = rest[j]
        opt = t.split("=", 1)[0]
        if opt in ("--mirror", "--all", "--branches"):
            return "git push %s pushes every branch, including %s" % (opt, "/".join(sorted(protected)))
        if opt == "--prune":
            return "git push --prune can delete remote branches"
        if opt == "--repo":
            has_repo = True
        if t.startswith("-"):
            j += 2 if (opt in PUSH_OPTS_WITH_ARG and "=" not in t) else 1
            continue
        positionals.append(t)
        j += 1
    refspecs = positionals if has_repo else positionals[1:]
    for p in positionals:
        if "$" in p or "*" in p:
            return "git push target %r uses shell expansion or a glob; spell the branch out" % p
    if not refspecs:
        br = branch_now()
        if br and br.lower() in lowered:
            how = " (checked out earlier in this command)" if switched else ""
            return "git push with no refspec from the protected branch %s%s" % (br, how)
        return None
    for spec in refspecs:
        src, sep, dst = spec.rpartition(":")
        if not sep:
            src, dst = spec, spec
        target = branch_of_ref(dst)
        if target == "HEAD":
            target = branch_now() or "HEAD"
        if target.lower() in lowered:
            return "git push to the protected branch %s (refspec %r)" % (target, spec)
    for p in positionals[:1]:
        if branch_of_ref(p).lower() in lowered and not has_repo:
            return "git push names the protected branch %s" % p
    return None


def check_gh(argv):
    words = [t for t in argv[1:] if not t.startswith("-")]
    if "pr" in words:
        k = words.index("pr")
        nxt = words[k + 1] if k + 1 < len(words) else ""
        if nxt == "merge":
            return "gh pr merge: merging is a human decision"
        if nxt == "review" and any(t in ("--approve", "-a") or t.startswith("--approve") for t in argv):
            return "gh pr review --approve: the agent cannot approve pull requests"
    if words[:2] == ["repo", "sync"]:
        return "gh repo sync can force-update a remote branch"
    if words[:1] == ["api"]:
        method = _gh_method(argv)
        joined = " ".join(argv)
        if re.search(r"/merges?\b", joined) or re.search(r"merge", joined, re.I) and "graphql" in joined:
            return "gh api merge endpoint: merging is a human decision"
        if method != "GET" and re.search(r"git/refs|/protection\b|/rulesets\b|/branches/", joined):
            return "gh api %s on refs, branch protection or rulesets" % method
    return None


# Unattended automation (rung 4/5 agent workflows) is switched on by a person:
# the AGENT_ENABLED kill switch, the agent:ready label, and the agent-*
# workflows. These names match the templates in rung4/ and rung5/.
AUTOMATION_VAR = "agent_enabled"
AUTOMATION_LABEL = "agent:ready"
AUTOMATION_WORKFLOW_PREFIX = "agent-"
GH_LABEL_OPTS = {"--add-label", "--label", "-l"}
GH_WORKFLOW_OPTS_WITH_ARG = {"-f", "-F", "--field", "--raw-field", "-r", "--ref", "-R", "--repo", "--json"}
AUTOMATION_WHY = "a person turns automation on; ask a maintainer to run it"


def _gh_method(argv):
    method = "GET"
    for k, t in enumerate(argv):
        if t in ("-X", "--method") and k + 1 < len(argv):
            method = argv[k + 1].upper()
        elif t.startswith("--method="):
            method = t.split("=", 1)[1].upper()
        elif t in ("-f", "-F", "--field", "--raw-field", "--input") and method == "GET":
            method = "POST"
    return method


def _gh_labels(argv):
    """Label names passed with --add-label / --label / -l (comma lists split)."""
    out = []
    for k, t in enumerate(argv):
        opt, eq, val = t.partition("=")
        if opt in GH_LABEL_OPTS and eq:
            out.extend(val.split(","))
        elif t in GH_LABEL_OPTS and k + 1 < len(argv):
            out.extend(argv[k + 1].split(","))
    return [l.strip().lower() for l in out]


def _is_agent_workflow(tok):
    base = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base.startswith(AUTOMATION_WORKFLOW_PREFIX)


def check_automation(argv):
    """Reason when a gh command switches on or starts unattended automation
    (verdict approve). argv: a gh argv with --title/--body values dropped."""
    words = [t for t in argv[1:] if not t.startswith("-")]
    low = [t.lower() for t in argv]
    if words[:1] == ["variable"] and words[1:2] and words[1] in ("set", "delete", "remove"):
        if AUTOMATION_VAR in low:
            return "gh variable %s AGENT_ENABLED: %s" % (words[1], AUTOMATION_WHY)
        if words[1] == "set" and any(t.split("=", 1)[0] in ("-f", "--env-file") for t in argv):
            return "gh variable set from a file can set AGENT_ENABLED: " + AUTOMATION_WHY
    if words[:1] in (["issue"], ["pr"]) and words[1:2] and words[1] in ("edit", "create"):
        if AUTOMATION_LABEL in _gh_labels(argv):
            return "gh %s %s --label agent:ready: %s" % (words[0], words[1], AUTOMATION_WHY)
    if words[:1] == ["workflow"] and words[1:2] and words[1] in ("run", "enable"):
        k = argv.index(words[1]) + 1
        while k < len(argv) and argv[k].startswith("-"):
            k += 2 if argv[k] in GH_WORKFLOW_OPTS_WITH_ARG else 1
        if k < len(argv) and _is_agent_workflow(argv[k]):
            return "gh workflow %s of an agent-* workflow: %s" % (words[1], AUTOMATION_WHY)
    if words[:1] == ["api"] and _gh_method(argv) != "GET":
        joined = " ".join(low)
        if re.search(r"(^|/)variables(/|\?|\s|$)", joined):
            return "gh api write to repository variables: " + AUTOMATION_WHY
        if re.search(r"/issues/[^/\s]+(/labels)?(\?|\s|$)", joined) and (
                AUTOMATION_LABEL in joined or "--input" in low):
            return "gh api adds the agent:ready label: " + AUTOMATION_WHY
        if re.search(r"actions/workflows/agent-[^/\s]*/(dispatches|enable)\b", joined):
            return "gh api starts or enables an agent-* workflow: " + AUTOMATION_WHY
    return None


def check_command_text(text, cwd, protected, depth=0, roots=None):
    """(verdict, reason) for the shell command text, verdict in allow /
    approve / block; the strictest finding wins. roots: repository roots for
    the protected-path checks (None skips them)."""
    if depth > 4:
        return "block", "command nests shells too deeply to analyze"
    text = strip_heredocs(text)
    for rx, what in TEXT_PATTERNS:
        if rx.search(text):
            return "block", what
    tier2 = protected_tier(roots) if roots else None
    try:
        tokens = tokenize(text)
    except ValueError:
        if re.search(r"\b(push|merge)\b", text):
            return "block", "could not parse a command that mentions push or merge"
        low = text.lower()
        if roots and any(d.rsplit("/", 1)[-1] in low for d in _hard_dirs()):
            return "block", "could not parse a command that mentions a never-agent-editable path"
        if tier2 and any(_mentions(low, d) for d in tier2.names):
            return "approve", "could not parse a command that mentions a protected path"
        return "allow", ""
    writes = []
    cmds = split_commands(tokens, writes)
    approve = None
    if roots:
        why = check_hard_paths(cmds, writes, cwd, roots)
        if why:
            return "block", why
        why = check_hard_paths(cmds, writes, cwd, roots, tier2)
        if why:
            approve = why
    verdict, reason = check_commands(cmds, cwd, protected, depth, roots)
    if verdict == "block":
        return verdict, reason
    if approve:
        return "approve", approve
    return verdict, reason


def check_commands(cmds, cwd, protected, depth, roots):
    approve = None
    branches = {}  # repo dir -> branch checked out earlier in this command line
    for argv in cmds:
        argv = strip_prefix(argv)
        if not argv:
            continue
        name = os.path.basename(argv[0]).lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name in SHELLS or name == "eval":
            inner = None
            if name == "eval":
                inner = " ".join(argv[1:])
            else:
                for k, t in enumerate(argv[1:], 1):
                    if re.match(r"^-[A-Za-z]*c[A-Za-z]*$", t) or t in ("-Command", "-command"):
                        inner = " ".join(argv[k + 1:k + 2])
                        break
            if inner:
                v, why = check_command_text(inner, cwd, protected, depth + 1, roots)
                if v == "block":
                    return v, why
                if v == "approve" and not approve:
                    approve = why
            continue
        if is_interpreter(name):
            code = " ".join(argv[1:])
            if re.search(r"\bpush\b", code) and any(
                    re.search(r"\b%s\b" % re.escape(b), code) for b in protected):
                return "block", "inline interpreter code mentions push and a protected branch"
            if re.search(r"\bpr\b.{0,20}\bmerge\b", code):
                return "block", "inline interpreter code runs a pull request merge"
            continue
        if name in ("cd", "pushd") and len(argv) > 1 and not argv[1].startswith("-"):
            target = argv[1]
            if not re.search(r"[$*?\[{`]", target):
                target = os.path.expanduser(target)
                nxt = target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target)
                if os.path.isdir(nxt):  # created later in this line: keep the old cwd
                    cwd = nxt
            continue
        if name == "git":
            why = check_git(argv, cwd, protected, branches)
        elif name == "gh":
            gh = drop_text_args(argv)
            why = check_gh(gh)
            auto = None if why else check_automation(gh)
            if auto and not approve:
                approve = auto
        else:
            why = None
        if why:
            return "block", why
    return ("approve", approve) if approve else ("allow", "")


# --------------------------------------------------------------------------
# agent adapters: the only code that knows a specific agent's hook protocol
# --------------------------------------------------------------------------

def parse_claude(raw):
    """Claude Code PreToolUse JSON: tool_name, tool_input, cwd on stdin;
    project root in $CLAUDE_PROJECT_DIR. Returns the neutral request."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        raise ValueError("tool_input is not an object")
    paths = [ti[k] for k in ("file_path", "notebook_path") if ti.get(k)]
    return {
        "tool": data.get("tool_name", ""),
        "paths": paths,
        "command": ti.get("command"),
        "cwd": data.get("cwd") or os.getcwd(),
        "project_dir": os.environ.get("CLAUDE_PROJECT_DIR") or None,
        "mode": data.get("permission_mode") or "",
    }


# Sentences shared by the respond_* adapters (text must stay identical).
HUMAN_OWNS = "A human owns this change. Stop and ask; do not try another route."
APPROVE_ONLY = "Approve only if a human wants this change."
HUMAN_BY_HAND = "A human must make this change by hand, or approve it outside the agent."


def respond_claude(verdict, reason, req=None):
    """Exit 2 with the reason on stderr blocks the tool call. "approve" prints
    permissionDecision "ask": the user confirms in session, and runs with no
    one to answer (dontAsk, -p without a prompt host) deny it. The docs do not
    say a hook "ask" still prompts in bypassPermissions (only ask rules are
    listed), so that mode gets a block instead."""
    mode = (req or {}).get("mode")
    if verdict == "approve" and mode != "bypassPermissions":
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "harness guard: %s. %s" % (reason, APPROVE_ONLY)}},
                  sys.stdout)
        sys.stdout.write("\n")
        return 0
    if verdict != "allow":
        sys.stderr.write("BLOCKED by harness guard: %s\n" % reason)
        sys.stderr.write(HUMAN_OWNS + "\n")
        return 2
    return 0


# Codex apply_patch grammar (openai/codex codex-rs/apply-patch/src/parser.rs):
# "*** Add File: P", "*** Delete File: P", "*** Update File: P" optionally
# followed by "*** Move to: P". Every named path is a target.
PATCH_HEADERS = ("*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: ")


def patch_paths(text):
    """Target paths named in an apply_patch patch, or [] if it is not one."""
    if not isinstance(text, str) or "*** Begin Patch" not in text:
        return []
    out = []
    for line in text.splitlines():
        s = line.strip()
        for h in PATCH_HEADERS:
            if s.startswith(h.strip() + " ") or s == h.strip():
                p = s[len(h.strip()):].strip()
                if not p:
                    raise ValueError("patch header without a path: %r" % s)
                out.append(p)
    if not out:
        raise ValueError("patch names no file")
    return out


def _obj(value, what):
    """Tool arguments as a dict; a JSON string is decoded (Copilot may send one)."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {"command": value}
    if not isinstance(value, dict):
        raise ValueError("%s is not an object" % what)
    return value


def _path_args(ti):
    """File paths in tool arguments whose key names are not documented
    (Cursor preToolUse Write/Delete, Copilot create/edit). UNVERIFIED key
    names: take every string or list of strings under a key that mentions
    "path" or "file". Over-collecting only makes the guard stricter."""
    out = []
    for k, v in ti.items():
        lk = k.lower()
        if "path" not in lk and "file" not in lk:
            continue
        for item in (v if isinstance(v, list) else [v]):
            if isinstance(item, str) and item and "\n" not in item:
                out.append(item)
    return out


def _load(raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    return data


def parse_codex(raw):
    """Codex PreToolUse: tool_name ("Bash" or "apply_patch"), tool_input.command
    (shell text, or the whole patch for apply_patch; no file_path field), cwd.
    No project-dir variable; the root is git toplevel of cwd."""
    data = _load(raw)
    ti = _obj(data.get("tool_input"), "tool_input")
    cmd = ti.get("command")
    if isinstance(cmd, list):  # defensive: argv form
        cmd = " ".join(shlex.quote(str(c)) for c in cmd)
    return {"tool": data.get("tool_name", ""), "paths": patch_paths(cmd),
            "command": cmd, "cwd": data.get("cwd") or os.getcwd(),
            "project_dir": None, "mode": data.get("permission_mode") or ""}


def respond_exit2(verdict, reason, req=None):
    """Shared by agents whose pre-tool hook blocks on exit 2 with the reason on
    stderr and has no "ask a human" decision (Codex PreToolUse, Gemini
    BeforeTool). "approve" therefore becomes a block that tells the person to
    make the change themselves."""
    if verdict == "allow":
        return 0
    sys.stderr.write("BLOCKED by harness guard: %s\n" % reason)
    if verdict == "approve":
        sys.stderr.write("This agent cannot ask a human from a hook. %s\n" % HUMAN_BY_HAND)
    else:
        sys.stderr.write(HUMAN_OWNS + "\n")
    return 2


def parse_gemini(raw):
    """Gemini CLI BeforeTool: tool_name (write_file, replace, run_shell_command),
    tool_input with file_path or command, cwd; root in $GEMINI_PROJECT_DIR."""
    data = _load(raw)
    ti = _obj(data.get("tool_input"), "tool_input")
    paths = [ti["file_path"]] if ti.get("file_path") else []
    return {"tool": data.get("tool_name", ""), "paths": paths, "command": ti.get("command"),
            "cwd": data.get("cwd") or os.getcwd(),
            "project_dir": os.environ.get("GEMINI_PROJECT_DIR") or None, "mode": ""}


def parse_cursor(raw):
    """Cursor: preToolUse (tool_name Write/Delete, tool_input with the path;
    key UNVERIFIED) or beforeShellExecution ({command, cwd} at top level).
    Hooks run from the project root."""
    data = _load(raw)
    ti = _obj(data.get("tool_input"), "tool_input")
    cmd = data.get("command") if isinstance(data.get("command"), str) else ti.get("command")
    paths = _path_args(ti) or _path_args({k: v for k, v in data.items() if k == "file_path"})
    return {"tool": data.get("tool_name", "Shell" if cmd else ""), "paths": paths,
            "command": cmd, "cwd": data.get("cwd") or ti.get("working_directory") or os.getcwd(),
            "project_dir": None, "mode": ""}


def respond_cursor(verdict, reason, req=None):
    """Cursor permission hooks must always print a JSON verdict: with
    failClosed true, no output counts as a failure and blocks. preToolUse
    accepts "ask" but does not enforce it, so "approve" is a deny."""
    if verdict == "allow":
        json.dump({"permission": "allow"}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    msg = "BLOCKED by harness guard: %s. " % reason + (
        HUMAN_BY_HAND if verdict == "approve" else HUMAN_OWNS)
    json.dump({"permission": "deny", "user_message": msg, "agent_message": msg}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def parse_copilot(raw):
    """Copilot CLI / cloud agent camelCase preToolUse: toolName (bash,
    powershell, create, edit, str_replace_editor, apply_patch...), toolArgs
    (may be a JSON string; path key UNVERIFIED), cwd. The snake_case form
    (PascalCase registration) is accepted too."""
    data = _load(raw)
    ti = _obj(data.get("toolArgs", data.get("tool_input")), "toolArgs")
    tool = data.get("toolName") or data.get("tool_name") or ""
    cmd = ti.get("command")
    paths = patch_paths(cmd) or patch_paths(ti.get("input")) or _path_args(ti)
    return {"tool": tool, "paths": paths, "command": cmd if not paths else None,
            "cwd": data.get("cwd") or os.getcwd(), "project_dir": None, "mode": ""}


def respond_copilot(verdict, reason, req=None):
    """stdout {"permissionDecision": allow|deny|ask, ...}. "ask" prompts in the
    interactive CLI and is denied in the cloud agent and autopilot, which
    matches "needs a human"."""
    if verdict == "allow":
        return 0
    if verdict == "approve":
        out = {"permissionDecision": "ask",
               "permissionDecisionReason": "harness guard: %s. %s" % (reason, APPROVE_ONLY)}
    else:
        out = {"permissionDecision": "deny",
               "permissionDecisionReason": "BLOCKED by harness guard: %s. %s" % (reason, HUMAN_OWNS)}
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


ADAPTERS = {
    "claude": (parse_claude, respond_claude),
    "codex": (parse_codex, respond_exit2),
    "gemini": (parse_gemini, respond_exit2),
    "cursor": (parse_cursor, respond_cursor),
    "copilot": (parse_copilot, respond_copilot),
}


def run_hook(fmt, kind):
    if fmt not in ADAPTERS:
        sys.stderr.write("BLOCKED by harness guard: no hook adapter for agent %r\n" % fmt)
        return 2
    parse, respond = ADAPTERS[fmt]
    try:
        req = parse(sys.stdin.read())
    except (ValueError, KeyError, TypeError) as exc:
        return respond("block", "could not read hook input (%s)" % exc)
    try:
        if kind == "path":
            verdict, reason = check_paths(req["paths"], req["cwd"], req["project_dir"])
        else:
            cmd = req.get("command")
            verdict, reason = "allow", ""
            # A patch arriving through the shell route (Codex, Copilot
            # apply_patch) is judged by the files it names.
            if req.get("paths"):
                verdict, reason = check_paths(req["paths"], req["cwd"], req["project_dir"])
            if cmd and verdict != "block":
                root = req["project_dir"] or git_toplevel(req["cwd"]) or req["cwd"]
                # Protected and HARD_BLOCKED paths through the shell, for every
                # agent; the strictest verdict wins. Skipped for a patch (its text is file content; its paths were judged).
                roots = None
                if not req.get("paths"):
                    roots = {r for r in (req["project_dir"], git_toplevel(req["cwd"]), root) if r}
                    roots |= {os.path.realpath(r) for r in roots}
                v, why = check_command_text(cmd, req["cwd"], protected_branches(root), roots=roots)
                rank = {"allow": 0, "approve": 1, "block": 2}
                if rank[v] > rank[verdict]:
                    verdict, reason = v, why
    except Misconfigured as exc:
        return respond("block", "MISCONFIGURED: %s" % exc, req)
    return respond(verdict, reason, req)


# --------------------------------------------------------------------------
# diff guard
# --------------------------------------------------------------------------

def diff_guard(args):
    root_p = run_git(["rev-parse", "--show-toplevel"])
    root = root_p.stdout.decode().strip()
    cfg_probe = load_config(root)
    default_branch = None
    if cfg_probe and isinstance(cfg_probe.get("default_branch"), str):
        default_branch = cfg_probe["default_branch"].strip() or None
    if not default_branch and "{" * 2 not in DEFAULT_BRANCH_FALLBACK:
        default_branch = DEFAULT_BRANCH_FALLBACK
    base = args.base or ("origin/%s" % default_branch if default_branch else None)
    if not base:
        raise Misconfigured("no base ref given and no default_branch in %s" % CONFIG_FILE)
    if run_git(["rev-parse", "--verify", "--quiet", base + "^{commit}"],
               cwd=root, check=False).returncode != 0:
        raise Misconfigured("base ref %s not found (fetch it, or use fetch-depth: 0 in CI)" % base)
    head = args.head or "HEAD"
    mb = run_git(["merge-base", base, head], cwd=root).stdout.decode().strip()
    if not mb:
        raise Misconfigured("no merge base between %s and %s (shallow clone?)" % (base, head))

    cfg = load_config(root, base_ref=base)
    if cfg is None:
        raise Misconfigured("%s not found on %s or in the working tree" % (CONFIG_FILE, base))
    limit = cfg.get("max_diff_lines")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise Misconfigured("max_diff_lines in %s must be a positive integer" % CONFIG_FILE)

    # Policy comes from the base branch so a PR cannot loosen the rules that
    # judge it; the PR's own copy only adds patterns (its "!" lines are dropped).
    base_text = git_show(base, PROTECTED_FILE, root)
    head_text = read_text(os.path.join(root, PROTECTED_FILE))
    if base_text is None and head_text is None:
        raise Misconfigured("%s not found on %s or in the working tree" % (PROTECTED_FILE, base))
    head_lines = [l for l in (head_text or "").splitlines() if not l.strip().startswith("!")]
    prot_lines = head_lines + (base_text or "").splitlines() + ALWAYS_PROTECTED + LOCKFILES
    if base_text is None:
        prot_lines = (head_text or "").splitlines() + ALWAYS_PROTECTED + LOCKFILES
    protected = compile_rules(prot_lines)
    gen_text = git_show(base, GENERATED_FILE, root) or ""
    excluded = compile_rules(GENERATED_DEFAULTS + LOCKFILES + gen_text.splitlines())

    diff_args = ["diff", "--numstat", "-z", "--no-renames", mb]
    if not args.worktree:
        diff_args.append(head)
    out = run_git(diff_args, cwd=root).stdout.decode("utf-8", "replace")
    files = []
    for rec in out.split("\0"):
        if not rec.strip():
            continue
        parts = rec.split("\t", 2)
        if len(parts) != 3:
            raise Misconfigured("unexpected git numstat record %r" % rec)
        a, d, path = parts
        n = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
        files.append((path, n))
    if args.worktree:
        untracked = run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=root)
        for path in untracked.stdout.decode("utf-8", "replace").split("\0"):
            if path:
                lines = read_text(os.path.join(root, path))
                files.append((path, len(lines.splitlines()) if lines else 0))

    counted, touched = 0, []
    for path, n in files:
        hit = match(protected, path)
        if hit:
            touched.append((path, hit))
        if not match(excluded, path):
            counted += n

    approved = os.environ.get(APPROVAL_ENV) == "1"
    problems = []
    for path, hit in touched:
        print("protected: %s (pattern %r)" % (path, hit))
    if touched:
        if args.allow_protected and approved:
            print("protected paths allowed: %s=1 (CODEOWNER approval on this head)" % APPROVAL_ENV)
        else:
            if args.allow_protected:
                print("--allow-protected ignored: %s is not 1" % APPROVAL_ENV)
            problems.append("%d protected path(s) changed without CODEOWNER approval" % len(touched))
    if counted > limit:
        problems.append("%d changed lines exceed max_diff_lines=%d (lockfiles and generated files excluded); split the change" % (counted, limit))
    summary = "%d files, %d counted lines, limit %d, base %s" % (len(files), counted, limit, base)
    if problems:
        print("HARNESS_DIFF_GUARD: FAIL (%s; %s)" % ("; ".join(problems), summary))
        return 1
    print("HARNESS_DIFF_GUARD: PASS (%s)" % summary)
    return 0


# --------------------------------------------------------------------------
# CODEOWNER approval (CI only)
# --------------------------------------------------------------------------

def codeowner_approval(args):
    """Print 1 when an individual CODEOWNER (not the author) approved the
    current head commit, else 0. Team owners (@org/team) cannot be resolved
    with a read-only token and are ignored; list individuals or rely on the
    ruleset's "Require review from Code Owners" for team ownership."""
    owners = set()
    text = read_text(args.codeowners) if args.codeowners else None
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        for tok in line.split()[1:]:
            if tok.startswith("@") and "/" not in tok:
                owners.add(tok[1:].lower())
    latest = {}
    for line in (read_text(args.reviews) or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            latest[parts[0].lower()] = (parts[1].upper(), parts[2])
    author = (args.author or "").lower()
    approvers = sorted(u for u, (state, sha) in latest.items()
                       if state == "APPROVED" and sha == args.head_sha
                       and u in owners and u != author)
    if approvers:
        sys.stderr.write("CODEOWNER approval on %s by: %s\n" % (args.head_sha, ", ".join(approvers)))
        print("1")
    else:
        sys.stderr.write("no individual CODEOWNER approval on %s\n" % args.head_sha)
        print("0")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    for name in ("hook-path", "hook-bash"):
        sp = sub.add_parser(name)
        sp.add_argument("--format", default="claude")
    sp = sub.add_parser("check-path")
    sp.add_argument("paths", nargs="+")
    sp = sub.add_parser("diff")
    sp.add_argument("--base")
    sp.add_argument("--head")
    sp.add_argument("--worktree", action="store_true",
                    help="compare the working tree (incl. untracked files) instead of HEAD")
    sp.add_argument("--allow-protected", action="store_true",
                    help="honored only when %s=1 is set by CI" % APPROVAL_ENV)
    sp = sub.add_parser("codeowner-approval")
    sp.add_argument("--reviews", required=True, help="TSV: login, state, commit_id")
    sp.add_argument("--codeowners")
    sp.add_argument("--head-sha", required=True)
    sp.add_argument("--author")
    args = ap.parse_args(argv)

    if args.cmd == "hook-path":
        return run_hook(args.format, "path")
    if args.cmd == "hook-bash":
        return run_hook(args.format, "bash")
    if args.cmd == "check-path":
        try:
            for p in args.paths:
                verdict, reason = check_paths([p], os.getcwd(), git_toplevel(os.getcwd()))
                label = {"allow": "ok", "approve": "PROTECTED", "block": "BLOCKED"}[verdict]
                print("%s\t%s" % (label, reason or p))
        except Misconfigured as exc:
            print("MISCONFIGURED: %s" % exc)
            return 2
        return 0
    if args.cmd == "diff":
        try:
            return diff_guard(args)
        except Misconfigured as exc:
            print("HARNESS_DIFF_GUARD: MISCONFIGURED (%s)" % exc)
            return 2
        except Exception as exc:  # a crash must still end with the status line
            print("HARNESS_DIFF_GUARD: MISCONFIGURED (internal error: %r)" % exc)
            return 2
    if args.cmd == "codeowner-approval":
        return codeowner_approval(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
