"""Rung 1, Legible: an agent can read the repo and find its commands."""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from .registry import (FAIL, NA, PASS, RECOMMENDED, UNKNOWN, Result, agent_check, check,
                       evaluator)
from .detect import COMMAND_KEYS, just_recipes, make_targets, npm_scripts

ROOT_DOCS = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")


# ---------------------------------------------------------------- doc helpers
def agent_docs(ctx) -> List[str]:
    """Root agent docs plus files they import with @path (one level)."""
    docs = [d for d in ROOT_DOCS if ctx.isfile(d)]
    for d in list(docs):
        for _, target in imports_of(ctx, d):
            if target and ctx.isfile(target) and target not in docs:
                docs.append(target)
    return docs


def _outside_code(lines: List[str]):
    """Yield (lineno, line) for lines outside fenced code blocks."""
    fence = False
    for i, l in enumerate(lines, 1):
        if re.match(r"^\s*(```|~~~)", l):
            fence = not fence
            continue
        if not fence:
            yield i, l


def code_segments(lines: List[str]):
    """Yield (lineno, text) for fenced-block lines and inline code spans."""
    fence = False
    for i, l in enumerate(lines, 1):
        if re.match(r"^\s*(```|~~~)", l):
            fence = not fence
            continue
        if fence:
            yield i, l
        else:
            for m in re.finditer(r"`([^`\n]+)`", l):
                yield i, m.group(1)


def imports_of(ctx, rel: str) -> List[Tuple[int, Optional[str]]]:
    """@path imports outside code, resolved relative to the importing file."""
    out = []
    base = os.path.dirname(rel)
    for i, l in _outside_code(ctx.lines(rel)):
        no_inline = re.sub(r"`[^`]*`", "", l)
        for m in re.finditer(r"(?:^|\s)@([\w./~-]+\.[\w]+|[\w./-]*AGENTS\.md)", no_inline):
            p = m.group(1)
            if p.startswith("~") or p.startswith("/"):
                out.append((i, None))
                continue
            joined = os.path.normpath(os.path.join(base, p)).replace(os.sep, "/")
            out.append((i, joined))
    return out


# ---------------------------------------------------------------- L1 checks
@check("L1.agent_doc", 1,
       "A file that tells the agent how to build and test (agent instructions file: AGENTS.md or CLAUDE.md)",
       fix="Create AGENTS.md at the repo root with your setup, build and test commands, and a CLAUDE.md containing the line @AGENTS.md (the advance skill does both)",
       why="Without it, the agent guesses how your project works and often guesses wrong.",
       how="Looks for AGENTS.md, CLAUDE.md or .claude/CLAUDE.md at the repo root.")
def agent_doc(ctx) -> Result:
    found = [d for d in ROOT_DOCS if ctx.isfile(d)]
    if found:
        return Result(PASS, ", ".join(d + ctx.uncommitted(d) for d in found))
    return Result(FAIL, "no AGENTS.md, CLAUDE.md or .claude/CLAUDE.md at the repo root")


agent_check("L1.agent_doc_wired", 1,
       "The agent actually reads AGENTS.md (CLAUDE.md imports it with @AGENTS.md)",
       fix="Add the line @AGENTS.md to CLAUDE.md",
       why="Claude Code skips AGENTS.md when a CLAUDE.md exists, so your instructions are silently ignored.",
       how="If AGENTS.md exists, checks that CLAUDE.md or .claude/CLAUDE.md has an @AGENTS.md import outside code blocks.")


@evaluator("L1.agent_doc_wired", "claude")
def _wired_claude(ctx) -> Result:
    if not ctx.isfile("AGENTS.md"):
        return Result(NA, "no AGENTS.md")
    claude_docs = [d for d in ("CLAUDE.md", ".claude/CLAUDE.md") if ctx.isfile(d)]
    if not claude_docs:
        return Result(FAIL, "AGENTS.md exists but no CLAUDE.md imports it",
                      "Create CLAUDE.md containing the line `@AGENTS.md`")
    for d in claude_docs:
        for line, target in imports_of(ctx, d):
            if target == "AGENTS.md":
                return Result(PASS, "%s:%d imports @AGENTS.md" % (d, line))
    return Result(FAIL, "%s does not import @AGENTS.md (Claude Code ignores AGENTS.md when CLAUDE.md exists)"
                  % ", ".join(claude_docs))


@check("L1.agent_doc_size", 1,
       "Instruction files the agent loads every time are short (200 lines or fewer each)",
       fix="Shorten CLAUDE.md / AGENTS.md to under 200 lines by moving detail into files under docs/ that they link to",
       why="Long instruction files use up the agent's attention, and it starts missing the rules that matter.",
       how="Counts lines in CLAUDE.md, .claude/CLAUDE.md and AGENTS.md.",
       severity=RECOMMENDED)
def agent_doc_size(ctx) -> Result:
    docs = [d for d in ROOT_DOCS if ctx.isfile(d)]
    if not docs:
        return Result(NA, "no root agent doc")
    sizes = [(d, len(ctx.lines(d))) for d in docs]
    big = [(d, n) for d, n in sizes if n > 200]
    ev = ", ".join("%s %d lines" % s for s in sizes)
    return Result(FAIL if big else PASS, ev)


COMMAND_RE = re.compile(
    r"(?:(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?[\w:.-]+|npx\s+\S+|make\s+[\w.-]+|just\s+[\w-]+"
    r"|(?:uv|poetry|pipenv|pdm)\s+run\s+\S+|uv\s+sync|python3?\s+-m\s+\w+|pytest\b|tox\b|nox\b"
    r"|go\s+(?:test|build|run|vet)\b|cargo\s+(?:test|build|run|clippy|check|nextest)\b"
    r"|bundle\s+exec\s+\w+|bin/rails\s+\w+|(?:\./)?mvnw?\s+[-\w:]+|(?:\./)?gradlew?\s+[-\w:]+"
    r"|composer\s+\w+|vendor/bin/\w+|\.harness/bin/[\w.-]+)")
TESTY = re.compile(r"test|pytest|rspec|spec|tox|nox|phpunit|pest|gates\.sh|\bcheck\b|\bci\b|verify")


@check("L1.commands_documented", 1,
       "The instructions file names the real test command",
       fix="Add a Commands section to AGENTS.md with the exact test command in backticks, for example `npm test`",
       why="Without a written test command, the agent may skip testing or run the wrong thing.",
       how="Searches code spans and code blocks in the agent docs (and files they import) for a test command, matching config commands.test, the detected test command or a known test runner.")
def commands_documented(ctx) -> Result:
    docs = agent_docs(ctx)
    if not docs:
        return Result(FAIL, "no agent doc to read commands from")
    cfg_cmds = ctx.harness_config().get("commands", {})
    known = []
    for src in (cfg_cmds if isinstance(cfg_cmds, dict) else {}, ctx.detected["commands"]):
        for k in ("test", "build"):
            v = src.get(k)
            if isinstance(v, str) and v:
                known.append(v)
    found_any = None
    for d in docs:
        for line, seg in code_segments(ctx.lines(d)):
            for k in known:
                if k in seg and TESTY.search(k):
                    return Result(PASS, "%s:%d names `%s`" % (d, line, k))
            for m in COMMAND_RE.finditer(seg):
                if TESTY.search(m.group(0)):
                    return Result(PASS, "%s:%d names `%s`" % (d, line, m.group(0)))
                found_any = found_any or "%s:%d `%s`" % (d, line, m.group(0))
    if found_any:
        return Result(FAIL, "commands found (%s) but no test command" % found_any,
                      "Add the test command to the Commands section of AGENTS.md")
    return Result(FAIL, "no build or test command in a code span in %s" % ", ".join(docs))


PATH_EXT = re.compile(r"\.(md|json|jsonl|ya?ml|toml|txt|py|sh|js|mjs|cjs|ts|tsx|jsx|go|rs|rb|"
                      r"php|java|kt|cfg|ini|conf|lock|sql|html|css)$")


def _path_candidates(ctx, rel: str):
    lines = ctx.lines(rel)
    for i, l in _outside_code(lines):
        for m in re.finditer(r"`([^`\n]+)`", l):
            yield i, m.group(1).strip(), False
        for m in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", l):
            yield i, m.group(1).strip(), True


def _missing_path(ctx, doc: str, tok: str, is_link: bool) -> Optional[str]:
    tok = tok.split("#", 1)[0] if is_link else tok
    tok = re.sub(r":\d+(?::\d+)?$", "", tok).rstrip(".,;:)")
    if not tok or re.search(r"://|^mailto:|[*{}<>$ ]|^[-~/@]|^\.\.?$", tok):
        return None
    if not re.match(r"^[\w.][\w./+-]*$", tok):
        return None
    has_ext = PATH_EXT.search(tok) is not None
    if not has_ext and "/" not in tok and not is_link:
        return None
    base = os.path.dirname(doc)
    cands = [tok, os.path.normpath(os.path.join(base, tok)).replace(os.sep, "/")]
    if any(ctx.exists(c.rstrip("/")) for c in cands):
        return None
    if "/" not in tok:
        # bare file name: accept if it exists anywhere in the tree
        if any(f.rsplit("/", 1)[-1] == tok for f in ctx.files):
            return None
        return tok if is_link else None
    if not has_ext and not is_link:
        first = tok.split("/", 1)[0]
        if not ctx.exists(first):
            return None  # probably not a path (branch name, owner/repo, mime type)
    return tok


@check("L1.refs_resolve", 1,
       "Files and commands named in the instructions file really exist",
       fix="Edit AGENTS.md to fix or delete each stale path, make target or package script listed in evidence",
       why="When the instructions point at files or commands that are gone, the agent loses trust in them or follows them into errors.",
       how="Checks paths in backticks and links, `make X`, `just X` and `npm run X` style commands in the agent docs against the files, Makefile targets, justfile recipes and package.json scripts.")
def refs_resolve(ctx) -> Result:
    docs = agent_docs(ctx)
    if not docs:
        return Result(NA, "no agent doc")
    missing: List[str] = []
    mk = ctx.find_ci("Makefile", "GNUmakefile", "makefile")
    targets = make_targets(ctx.read_text(mk) or "") if mk else None
    jf = ctx.find_ci("justfile", ".justfile")
    recipes = just_recipes(ctx.read_text(jf) or "") if jf else None
    scripts: Dict[str, str] = {}
    for f in ctx.files:
        if f.rsplit("/", 1)[-1] == "package.json":
            scripts.update(npm_scripts(ctx, f))
    checked = 0
    for d in docs:
        for line, tok, is_link in _path_candidates(ctx, d):
            bad = _missing_path(ctx, d, tok, is_link)
            if bad:
                missing.append("%s:%d path %s" % (d, line, bad))
            checked += 1
        for line, seg in code_segments(ctx.lines(d)):
            for m in re.finditer(r"(?<![\w-])make\s+(?:-\S+\s+)*([A-Za-z0-9][\w.-]*)(?![\w=])", seg):
                checked += 1
                if targets is None or m.group(1) not in targets:
                    missing.append("%s:%d make target %s" % (d, line, m.group(1)))
            for m in re.finditer(r"(?<![\w-])just\s+([A-Za-z0-9][\w-]*)", seg):
                checked += 1
                if recipes is None or m.group(1) not in recipes:
                    missing.append("%s:%d just recipe %s" % (d, line, m.group(1)))
            for m in re.finditer(r"(?<![\w-])(?:npm|pnpm|yarn|bun)\s+(?:run\s+([\w:.-]+)|(test)\b)", seg):
                name = m.group(1) or m.group(2)
                checked += 1
                if name not in scripts:
                    missing.append("%s:%d package script %s" % (d, line, name))
    if missing:
        more = " (+%d more)" % (len(missing) - 5) if len(missing) > 5 else ""
        return Result(FAIL, "; ".join(missing[:5]) + more)
    return Result(PASS, "%d references checked in %s" % (checked, ", ".join(docs)))


@check("L1.test_cmd", 1,
       "There is one command that runs the tests",
       fix="Add a test entry point (a \"test\" script in package.json or a `test:` target in the Makefile) and set commands.test in .harness/config.json",
       why="If the agent cannot find how to run tests, it cannot check its own work.",
       how="Uses commands.test from .harness/config.json, else the guess from package.json scripts, Makefile/justfile targets, pyproject, go.mod, Cargo.toml, Gemfile, pom.xml/Gradle or composer.json.")
def test_cmd(ctx) -> Result:
    cfg = ctx.harness_config().get("commands", {})
    if isinstance(cfg, dict) and isinstance(cfg.get("test"), str) and cfg["test"]:
        return Result(PASS, "config commands.test = `%s`" % cfg["test"])
    t = ctx.detected["commands"].get("test")
    if t:
        return Result(PASS, "`%s` (from %s)" % (t, ctx.detected["command_sources"].get("test", "?")))
    return Result(FAIL, "no test script, target or runner config found")


LOCKS = {
    "node": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
             "bun.lockb", "bun.lock"),
    "python": ("uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock", "pylock.toml"),
    "rust": ("Cargo.lock",),
    "ruby": ("Gemfile.lock",),
    "php": ("composer.lock",),
}


@check("L1.manifest_lockfile", 1,
       "Dependency versions are locked (a lockfile is committed)",
       fix="Run your package manager's install (for example `npm install`, `uv lock`, `cargo generate-lockfile`) and commit the lockfile it writes",
       why="Without a lockfile, the agent's machine can install different versions than yours and tests fail for reasons nobody can see.",
       how="For each detected ecosystem, looks for its lockfile (package-lock.json, yarn.lock, pnpm-lock.yaml, bun.lock, uv.lock, poetry.lock, Cargo.lock, go.sum, Gemfile.lock, composer.lock) or pinned requirements*.txt.")
def manifest_lockfile(ctx) -> Result:
    eco = [e for e in ctx.detected["ecosystems"] if e not in ("make", "just", "kotlin")]
    if not eco:
        return Result(NA, "no dependency manifest detected; nothing to lock")
    ok, bad = [], []
    for e in eco:
        if e in LOCKS:
            hit = next((l for l in LOCKS[e] if ctx.isfile(l)), None)
            if not hit and e == "python":
                reqs = [f for f in ctx.files if "/" not in f and re.match(r"requirements.*\.txt$", f)]
                pinned = [r for r in reqs if any("==" in l for l in ctx.lines(r))]
                hit = pinned[0] + " (pinned)" if pinned else None
                if not hit and not ctx.read_text("pyproject.toml") and reqs:
                    bad.append("python: %s has no == pins" % reqs[0])
                    continue
            (ok if hit else bad).append("%s: %s" % (e, hit or "no lockfile"))
        elif e == "go":
            gomod = ctx.read_text("go.mod") or ""
            if "require" not in gomod or ctx.isfile("go.sum"):
                ok.append("go: %s" % ("go.sum" if ctx.isfile("go.sum") else "no dependencies"))
            else:
                bad.append("go: go.sum missing")
        elif e == "java":
            ok.append("java: no lockfile convention (versions pinned in build file)")
    if bad:
        return Result(FAIL, "; ".join(bad + ok))
    return Result(PASS, "; ".join(ok))


# Phrases from the agent-readiness scanner (MIT, headless.no_setup_prompts),
# trimmed to ones that indicate a human must act during setup.
GUI_PHRASES = re.compile(
    r"click here|log in to the (?:website|dashboard)|sign in with (?:google|github)|"
    r"sign in to your account|authorize the application|complete the wizard|follow the prompts|"
    r"device code flow|oauth callback", re.I)
INTERACTIVE_SH = re.compile(
    r"\bread\s+(?:-\w*[ps]\w*)\b|^\s*select\s+\w+\s+in\b|"
    r"\b(?:gh auth login|npm (?:login|adduser)|firebase login|heroku login|vercel login|"
    r"netlify login|az login|gcloud auth login|aws configure(?!\s+(?:set|get|list|export))|"
    r"docker login(?!.*--password-stdin))\b")


@check("L1.setup_headless", 1,
       "Setup runs without a person typing answers or logging in (best guess)",
       fix="Change the setup script or README step named in evidence to read values from environment variables or flags instead of prompting",
       why="An agent running alone cannot answer a prompt or click a login page, so setup just hangs.",
       how="Scans README, CONTRIBUTING, setup docs and setup/bootstrap/install scripts for prompts (read -p, select), interactive logins (gh auth login, npm login and similar) and click-through phrases.")
def setup_headless(ctx) -> Result:
    docs = [f for f in ctx.files if re.match(
        r"(?i)^(readme|contributing|install|setup)[^/]*\.(md|rst|txt)$|^docs/[^/]*(setup|install|getting.?started|develop)[^/]*\.md$", f)]
    docs += [d for d in ROOT_DOCS if ctx.isfile(d)]
    scripts = [f for f in ctx.files if re.search(
        r"(?i)(^|/)(setup|bootstrap|install|init)[^/]*\.(sh|bash)$|^(scripts|bin)/(setup|bootstrap|install)[^/.]*$", f)]
    hits = []
    for rel in docs:
        for i, l in enumerate(ctx.lines(rel), 1):
            m = GUI_PHRASES.search(l) or INTERACTIVE_SH.search(l)
            if m:
                hits.append("%s:%d `%s`" % (rel, i, m.group(0).strip()))
    for rel in scripts:
        for i, l in enumerate(ctx.lines(rel), 1):
            if l.lstrip().startswith("#"):
                continue
            m = INTERACTIVE_SH.search(l)
            if m:
                hits.append("%s:%d `%s`" % (rel, i, m.group(0).strip()))
    if hits:
        return Result(FAIL, "; ".join(hits[:5]) + (" (+%d more)" % (len(hits) - 5) if len(hits) > 5 else ""))
    return Result(PASS, "scanned %d docs and %d setup scripts; no prompts or logins found"
                  % (len(docs), len(scripts)))


ENV_READS = [
    r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]+)", r"os\.environ\.get\(\s*[\"']([A-Z][A-Z0-9_]+)",
    r"os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]+)", r"process\.env\.([A-Z][A-Z0-9_]+)",
    r"process\.env\[\s*[\"']([A-Z][A-Z0-9_]+)", r"import\.meta\.env\.([A-Z][A-Z0-9_]+)",
    r"ENV(?:\.fetch\(|\[)\s*[\"']([A-Z][A-Z0-9_]+)", r"os\.(?:Getenv|LookupEnv)\(\s*\"([A-Z][A-Z0-9_]+)",
    r"env::var(?:_os)?\(\s*\"([A-Z][A-Z0-9_]+)", r"getenv\(\s*[\"']([A-Z][A-Z0-9_]+)",
    r"\$_ENV\[\s*[\"']([A-Z][A-Z0-9_]+)", r"System\.getenv\(\s*\"([A-Z][A-Z0-9_]+)",
]
ENV_RE = re.compile("|".join(ENV_READS))
SYSTEM_ENV = {"HOME", "PATH", "PWD", "USER", "SHELL", "TERM", "TMPDIR", "TEMP", "TMP", "CI",
              "NODE_ENV", "LANG", "LC_ALL", "DEBUG", "PORT", "HOSTNAME", "EDITOR", "VIRTUAL_ENV",
              "PYTHONPATH", "GOPATH", "NO_COLOR", "FORCE_COLOR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
              "XDG_DATA_HOME", "APPDATA", "USERPROFILE", "LOGNAME", "COLUMNS", "TZ", "BASE_URL",
              "MODE", "DEV", "PROD", "SSR"}
SRC_EXT = re.compile(r"\.(py|js|mjs|cjs|ts|tsx|jsx|rb|go|rs|php|java|kt)$")
TEST_SEG = re.compile(r"(^|/)(tests?|spec|__tests__|fixtures?|examples?|testdata|docs)/|"
                      r"(_test\.\w+|\.test\.\w+|\.spec\.\w+|test_[^/]*\.py)$")
ENV_EXAMPLES = (".env.example", ".env.sample", ".env.template", ".env.dist", "example.env",
                ".env.defaults")


# Harness, CI and agent config read their own runtime variables (for example
# the agent's project dir); those are not app settings for .env.example.
AGENT_CFG_DIRS = (".harness/", ".github/", ".claude/", ".codex/", ".cursor/", ".gemini/",
                  ".agents/", ".opencode/", ".windsurf/", ".continue/")


@check("L1.env_example_parity", 1,
       "Every environment variable the code reads is listed in .env.example",
       fix="Add each missing variable name with a safe placeholder value to .env.example",
       why="The agent cannot run the app if it does not know which settings it needs.",
       how="Finds env reads in source code (os.getenv, process.env, ENV[], os.Getenv, env::var and similar, tests and harness/CI/agent config dirs excluded) and compares the names with .env.example.",
       severity=RECOMMENDED)
def env_example_parity(ctx) -> Result:
    names: Dict[str, str] = {}
    scanned = 0
    for f in ctx.files:
        if scanned >= 3000:
            break
        if not SRC_EXT.search(f) or TEST_SEG.search(f) or f.startswith(AGENT_CFG_DIRS):
            continue
        scanned += 1
        for i, l in enumerate(ctx.lines(f), 1):
            for m in ENV_RE.finditer(l):
                n = next(g for g in m.groups() if g)
                if n not in SYSTEM_ENV and not n.startswith(("GITHUB_", "RUNNER_", "npm_")):
                    names.setdefault(n, "%s:%d" % (f, i))
    if not names:
        return Result(NA, "no env var reads found in %d source files" % scanned)
    example = ctx.first_existing(*ENV_EXAMPLES)
    keys = set()
    if example:
        for l in ctx.lines(example):
            m = re.match(r"^\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", l)
            if m:
                keys.add(m.group(1))
    missing = sorted(n for n in names if n not in keys)
    if not missing:
        return Result(PASS, "%d vars all listed in %s" % (len(names), example))
    shown = ", ".join("%s (%s)" % (n, names[n]) for n in missing[:5])
    more = " (+%d more)" % (len(missing) - 5) if len(missing) > 5 else ""
    where = example or "no .env.example"
    return Result(FAIL, "%s; missing: %s%s" % (where, shown, more))


@check("L1.git_history", 1,
       "The project is in git and has commits",
       fix="Run `git init`, then `git add -A && git commit -m \"initial commit\"`",
       why="Without history the agent cannot see why code changed, and you cannot easily undo its changes.",
       how="Runs `git rev-list --count HEAD` (read-only).",
       severity=RECOMMENDED)
def git_history(ctx) -> Result:
    if not ctx.git_available:
        return Result(UNKNOWN, "git not on PATH", "Install git")
    if not ctx.is_git:
        return Result(FAIL, "not a git repository")
    r = ctx.git("rev-list", "--count", "HEAD")
    if not r or r[0] != 0:
        return Result(FAIL, "no commits yet")
    n = int(r[1].strip() or 0)
    return Result(PASS if n >= 1 else FAIL, "%d commits on HEAD" % n)


# High-confidence patterns, adapted from the agent-readiness scanner
# (MIT, secrets.basic_scan) with a few additions (Anthropic, OpenAI project,
# npm, GitLab, Slack webhook).
SECRET_RULES = [
    ("aws_access_key_id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("aws_secret_access_key", r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}\b"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
    ("github_pat", r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    ("gitlab_pat", r"\bglpat-[A-Za-z0-9_-]{20}\b"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("slack_webhook", r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{20,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("stripe_live_key", r"\b[sr]k_live_[0-9a-zA-Z]{24,}\b"),
    ("anthropic_api_key", r"\bsk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{40,}"),
    ("openai_project_key", r"\bsk-proj-[A-Za-z0-9_-]{40,}"),
    ("npm_token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("private_key", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA |ENCRYPTED )?PRIVATE KEY-----"),
]
SECRET_RE = re.compile("|".join("(?P<%s>%s)" % (n, p.replace("(?i)", "")) for n, p in SECRET_RULES))
SECRET_RE_I = re.compile(SECRET_RULES[1][1])  # the one case-insensitive rule
SKIP_SECRET = re.compile(r"(^|/)(fixtures?|testdata|__fixtures__|__snapshots__)/|"
                         r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|"
                         r"uv\.lock|composer\.lock|Gemfile\.lock|go\.sum)$|"
                         r"\.(png|jpe?g|gif|ico|pdf|zip|gz|tgz|woff2?|ttf|otf|mp4|mov|wasm|jar)$", re.I)
# Documented placeholder keys and the inline opt-outs gitleaks and
# detect-secrets already use, so test fixtures can be marked as intentional.
ALLOW_LINE = re.compile(r"EXAMPLE|gitleaks:allow|pragma: allowlist secret|nosec")
SCAN_BUDGET = 64 * 1024 * 1024
# Per-user agent settings: "don't ask again" approvals store full command
# lines there, inline secrets included, so they must never be committed.
LOCAL_AGENT_FILES = (".claude/settings.local.json",)


@check("L1.no_secrets", 1,
       "No passwords or API keys are committed",
       fix="Rotate the leaked key first, then delete it from the file and from git history (git filter-repo), and add gitleaks",
       why="Anything committed is readable by the agent and everyone with access, and it stays in history after you delete it.",
       how="Regex scan of tracked and new files for high-confidence key formats (AWS, GitHub, GitLab, Slack, Google, Stripe, Anthropic, OpenAI, npm, private keys), tracked .env files and tracked .claude/settings.local.json. Evidence shows only file:line and the rule name.")
def no_secrets(ctx) -> Result:
    findings: List[str] = []
    local = [f for f in LOCAL_AGENT_FILES if f in ctx.tracked]
    env_files = [f for f in ctx.tracked
                 if re.search(r"(^|/)\.env(\.[\w-]+)?$", f)
                 and not re.search(r"\.(example|sample|template|dist|defaults)$", f)]
    used = scanned = skipped_big = 0
    over_budget = 0
    for f in ctx.files:
        if SKIP_SECRET.search(f):
            continue
        p = ctx.abspath(f)
        if os.path.islink(p):
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if size > 512 * 1024:
            skipped_big += 1
            continue
        if used + size > SCAN_BUDGET:
            over_budget += 1
            continue
        text = ctx.read_text(f)
        if text is None:
            continue
        used += size
        scanned += 1
        if not ("KEY" in text or "_" in text or "-" in text):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = SECRET_RE.search(line)
            rule = m.lastgroup if m else ("aws_secret_access_key" if SECRET_RE_I.search(line) else None)
            if rule and ALLOW_LINE.search(line):
                continue
            if rule:
                # Never echo the secret: location and rule name only.
                findings.append("%s:%d %s" % (f, i, rule))
                if len(findings) >= 50:
                    break
        if len(findings) >= 50:
            break
    if local:
        return Result(FAIL, "per-user agent settings tracked: %s" % ", ".join(local)
                      + ("; " + "; ".join(findings[:5]) if findings else ""),
                      "untrack it and add it to .gitignore (git rm --cached %s)" % local[0])
    parts = findings[:8]
    if env_files:
        parts.append("tracked env file(s): %s" % ", ".join(env_files[:3]))
    if parts:
        more = " (+%d more)" % (len(findings) - 8) if len(findings) > 8 else ""
        return Result(FAIL, "; ".join(parts) + more)
    note = "scanned %d files" % scanned
    if skipped_big:
        note += ", skipped %d files over 512KB" % skipped_big
    if ctx.listing_truncated or over_budget:
        return Result(UNKNOWN, note + "; scan incomplete (%d files over the size budget)" % over_budget,
                      "Run a full scanner such as gitleaks on this repo")
    return Result(PASS, note + "; no high-confidence patterns")


def _ignored(ctx, probe: str) -> Optional[bool]:
    if not ctx.is_git:
        return None
    r = ctx.git("-c", "core.excludesFile=" + os.devnull, "check-ignore", "-q", "--no-index", probe)
    if r is None:
        return None
    return r[0] == 0


def _ignored_many(ctx, probes: List[str]) -> Dict[str, Optional[bool]]:
    """_ignored for every probe with one `git check-ignore` call.

    Falls back to one call per probe when the batched call errors (exit
    other than 0 or 1), so a single bad path keeps its per-path answer.
    """
    if not ctx.is_git or not probes:
        return {p: None for p in probes}
    r = ctx.git("-c", "core.excludesFile=" + os.devnull, "check-ignore", "-z", "--no-index", "--", *probes)
    if r is None or r[0] not in (0, 1):
        return {p: _ignored(ctx, p) for p in probes}
    hit = set(r[1].split("\0"))
    return {p: p in hit for p in probes}


def _ignored_text(lines: List[str], name: str) -> bool:
    for l in lines:
        s = l.strip()
        if not s or s.startswith("#") or s.startswith("!"):
            continue
        s = s.lstrip("/").rstrip("/")
        if s.startswith("**/"):
            s = s[3:]
        if s in (name, name + "/*", name + "/**", name.split("/")[-1]) or (
                "*" in s and re.fullmatch(s.replace(".", r"\.").replace("*", ".*"), name)):
            return True
    return False


@check("L1.gitignore", 1,
       "Git ignores secrets, per-user settings and build output (.gitignore)",
       fix="Add the entries listed in evidence to .gitignore",
       why="Without these entries the agent can commit secrets, personal settings or huge build folders by accident.",
       how="Asks git whether probe paths (.env, node_modules/, __pycache__/, .venv/, target/, vendor/, .claude/settings.local.json) are ignored by the repo's own ignore files; falls back to reading .gitignore.",
       severity=RECOMMENDED)
def gitignore(ctx) -> Result:
    if not ctx.isfile(".gitignore"):
        return Result(FAIL, "no .gitignore")
    eco = ctx.detected["ecosystems"]
    probes = [(".env", ".env")]
    if "node" in eco:
        probes.append(("node_modules", "node_modules/x"))
    if "python" in eco:
        probes += [("__pycache__", "__pycache__/x.pyc"), (".venv", ".venv/x")]
    if "rust" in eco or ctx.detected["package_managers"].get("java", "").startswith("maven"):
        probes.append(("target", "target/x"))
    if "gradle" in ctx.detected["package_managers"].get("java", ""):
        probes += [(".gradle", ".gradle/x"), ("build", "build/x")]
    if "php" in eco:
        probes.append(("vendor", "vendor/x"))
    if ctx.isdir(".claude"):
        probes.append((".claude/settings.local.json", ".claude/settings.local.json"))
    gi = ctx.lines(".gitignore")
    missing = []
    answers = _ignored_many(ctx, [probe for _, probe in probes])
    for name, probe in probes:
        r = answers[probe]
        if r is None:
            r = _ignored_text(gi, name)
        if not r:
            missing.append(name)
    if missing:
        return Result(FAIL, "not ignored: %s" % ", ".join(missing))
    return Result(PASS, "covers %s" % ", ".join(n for n, _ in probes))


def validate_config(data) -> List[str]:
    errs = []
    if not isinstance(data, dict):
        return ["top level is not an object"]
    if data.get("schema") != 1:
        errs.append("schema must be 1")
    if not isinstance(data.get("default_branch"), str) or not data.get("default_branch"):
        errs.append("default_branch must be a non-empty string")
    cmds = data.get("commands")
    if not isinstance(cmds, dict):
        errs.append("commands must be an object")
    else:
        for k in COMMAND_KEYS:
            if not isinstance(cmds.get(k), str):
                errs.append("commands.%s must be a string" % k)
    cf = data.get("core_flow")
    if not isinstance(cf, dict) or not all(isinstance(cf.get(k), str) for k in ("description", "smoke_command")):
        errs.append("core_flow needs string description and smoke_command")
    for k in ("protected_paths", "human_only_areas", "maintainers"):
        v = data.get(k)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errs.append("%s must be a list of strings" % k)
    m = data.get("max_diff_lines")
    if not isinstance(m, int) or isinstance(m, bool) or m <= 0:
        errs.append("max_diff_lines must be a positive integer")
    if "agents" in data:
        a = data["agents"]
        if not isinstance(a, list) or not a or not all(isinstance(x, str) and x for x in a):
            errs.append("agents must be a non-empty list of strings")
    return errs


@check("L1.harness_config", 1,
       "The harness settings file .harness/config.json exists and is valid",
       fix="Run the advance skill, which asks you a few questions and writes .harness/config.json",
       why="Every later step reads its commands and branch name from this file, so nothing else can be set up without it.",
       how="Parses .harness/config.json and checks the schema: schema 1, default_branch, commands.*, core_flow, protected_paths, human_only_areas, max_diff_lines, maintainers, optional agents.")
def harness_config(ctx) -> Result:
    data, err = ctx.load_json(".harness/config.json")
    if err == "missing":
        return Result(FAIL, ".harness/config.json missing")
    if err:
        return Result(FAIL, ".harness/config.json %s" % err)
    errs = validate_config(data)
    if errs:
        return Result(FAIL, "; ".join(errs[:4]), "Fix .harness/config.json: " + errs[0])
    return Result(PASS, ".harness/config.json valid (agents: %s)%s"
                  % (", ".join(ctx.agents), ctx.uncommitted(".harness/config.json")))
