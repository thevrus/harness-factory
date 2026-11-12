"""Rung 2, Verifiable: one command says pass or fail, and the agent must use it."""
from __future__ import annotations

import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from .registry import (FAIL, NOT_MEASURED, PASS, RECOMMENDED, UNKNOWN, Result,
                       agent_check, check, evaluator)
from .context import has_marker
from . import claude_cfg, installed
from .workflows import is_agent_text, load_workflows, neutered_reason

GATES = ".harness/bin/gates.sh"
GATES_CONF = ".harness/gates.conf"
SMOKE = ".harness/bin/smoke.sh"
STUB = "HARNESS_STUB"


# ---------------------------------------------------------------- run helpers
_RUN_CACHE: Dict[Tuple[str, str], Result] = {}


def run_repo_script(ctx, rel: str, interpreter: Optional[str] = None,
                    timeout: Optional[int] = None) -> Result:
    """Execute one harness script (only under --run). Exit 0 is pass."""
    key = (ctx.root, rel)
    if key in _RUN_CACHE:
        return _RUN_CACHE[key]
    p = ctx.abspath(rel)
    ok, _ = ctx.executable(rel)
    argv = [interpreter, p] if interpreter else ([p] if ok or os.access(p, os.X_OK) else ["bash", p])
    env = dict(os.environ)
    env["HF_AUDIT"] = "1"
    try:
        proc = subprocess.run(argv, cwd=ctx.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout or ctx.run_timeout, env=env)
    except subprocess.TimeoutExpired:
        r = Result(FAIL, "%s timed out after %ds" % (rel, timeout or ctx.run_timeout))
    except OSError as e:
        r = Result(UNKNOWN, "%s could not start: %s" % (rel, e))
    else:
        out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        tail = out[-1][:160] if out else "(no output)"
        r = Result(PASS if proc.returncode == 0 else FAIL,
                   "%s exited %d; last line: %s" % (rel, proc.returncode, tail))
    _RUN_CACHE[key] = r
    return r


def gate_lines(ctx) -> Tuple[List[str], List[str]]:
    """(required real gates, stub or optional gates) from .harness/gates.conf.

    The rung 2 template ships `name|required|command` lines; other shapes are
    accepted too: one gate per line, or a shell-style REQUIRED...="a b" list. A line with
    HARNESS_STUB or an unresolved {{var}} is a stub; one saying `optional`
    is not required.
    """
    real, other = [], []
    for _, line in ctx.meaningful_lines(GATES_CONF):
        stub = STUB in line or "{{" in line
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[1].strip() in ("required", "optional"):
            # The shipped format: name|required|command
            if parts[1].strip() == "required" and parts[2].strip() and not stub:
                real.append(parts[0].strip())
            else:
                other.append(parts[0].strip())
            continue
        m = re.match(r"^(?:export\s+)?(REQUIRED\w*)\s*=\s*[\(\"']?([^\"')]*)", line)
        if m:
            names = m.group(2).split()
            (other if stub else real).extend(names)
            continue
        if re.match(r"^(?:export\s+)?[A-Z_][A-Z0-9_]*=", line) and "REQUIRED" not in line:
            continue  # unrelated shell setting
        if stub or re.search(r"(?i)\boptional\b", line):
            other.append(line)
        else:
            real.append(line)
    return real, other


# ---------------------------------------------------------------- V2 checks
TEST_FILE = re.compile(
    r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|[^/]+_test\.go|[^/]+\.(test|spec)\.[cm]?[jt]sx?|"
    r"[^/]+_spec\.rb|[^/]+Test\.(java|kt|php)|[^/]+Tests?\.cs)$|"
    r"(^|/)(tests?|__tests__|spec)/[^/]+\.(py|[cm]?[jt]sx?|rb|go|rs|php|sh|bats|ex|exs)$|"
    r"(^|/)src/test/")


@check("V2.tests_exist", 2,
       "The project has automated tests",
       fix="Add at least one test file (for example tests/test_smoke.py or src/app.test.ts) that runs with your test command",
       why="With no tests, a passing test run proves nothing and the agent's changes go unchecked.",
       how="Counts files that match common test naming (test_*.py, *_test.go, *.test.ts, *_spec.rb, *Test.java, files under tests/ or __tests__/), and Rust files with #[test].")
def tests_exist(ctx) -> Result:
    hits = [f for f in ctx.files if TEST_FILE.search(f)]
    if not hits and "rust" in ctx.detected["ecosystems"]:
        for f in ctx.files:
            if f.endswith(".rs") and "#[test]" in (ctx.read_text(f) or ""):
                hits.append(f)
                if len(hits) >= 3:
                    break
    if hits:
        return Result(PASS, "%d test files, e.g. %s" % (len(hits), ", ".join(hits[:3])))
    return Result(FAIL, "no test files found")


@check("V2.gates_script", 2,
       "One script decides pass or fail (.harness/bin/gates.sh with at least one real gate)",
       fix="Run the advance skill for rung 2 to install .harness/bin/gates.sh, then list a real required gate (your test command) in .harness/gates.conf",
       why="Without one command that says pass or fail, the agent decides for itself whether its work is done.",
       how="Checks .harness/bin/gates.sh exists, is committed as executable, has no HARNESS_STUB marker and matches the shipped script rendered with .harness/config.json (unless listed in config customized_scripts); and that .harness/gates.conf has at least one required gate that is not a stub.")
def gates_script(ctx) -> Result:
    if not ctx.isfile(GATES):
        return Result(FAIL, "%s missing" % GATES)
    ok, why = ctx.executable(GATES)
    if not ok:
        return Result(FAIL, why, "Run `chmod +x %s && git update-index --chmod=+x %s`" % (GATES, GATES))
    if has_marker(ctx, GATES, STUB):
        return Result(FAIL, "%s is still the shipped stub (contains %s)" % (GATES, STUB),
                      "Replace the stub in %s with the real gate runner from the rung 2 template" % GATES)
    bad = modified_guard(ctx, GATES)
    if bad:
        return bad
    if not ctx.isfile(GATES_CONF):
        return Result(FAIL, "%s missing" % GATES_CONF,
                      "Create %s and list your test command as a required gate" % GATES_CONF)
    real, other = gate_lines(ctx)
    if not real:
        return Result(FAIL, "%s has no required gate that is not a stub (%d stub/optional)"
                      % (GATES_CONF, len(other)),
                      "Edit %s: replace the stub gate with your real test command" % GATES_CONF)
    return Result(PASS, "%s; %d required gate(s) in %s%s" % (why, len(real), GATES_CONF,
                                                          ctx.uncommitted(GATES_CONF)))


@check("V2.gates_pass", 2,
       "The gates pass right now (needs --run)",
       fix="Run `.harness/bin/gates.sh`, fix what fails, then re-run the audit with --run",
       why="If the gates already fail before the agent starts, it cannot tell its own mistakes from old ones.",
       how="Only with --run: executes .harness/bin/gates.sh with a timeout; exit 0 is pass. Without --run it is not_measured.")
def gates_pass(ctx) -> Result:
    if not ctx.run:
        return Result(NOT_MEASURED, "needs --run (executes %s)" % GATES,
                      "Re-run the audit with --run")
    if not ctx.isfile(GATES):
        return Result(FAIL, "%s missing" % GATES)
    return run_repo_script(ctx, GATES)


@check("V2.smoke_defined", 2,
       "A quick check that the main feature still works (.harness/bin/smoke.sh is filled in)",
       fix="Edit .harness/bin/smoke.sh: replace the stub with a command that exercises your core flow, and remove the HARNESS_STUB marker",
       why="Tests can pass while the app itself is broken; a smoke check catches that.",
       how="Checks .harness/bin/smoke.sh exists, is executable and no longer contains the HARNESS_STUB marker.")
def smoke_defined(ctx) -> Result:
    if not ctx.isfile(SMOKE):
        return Result(FAIL, "%s missing" % SMOKE,
                      "Run the advance skill for rung 2 to install .harness/bin/smoke.sh")
    ok, why = ctx.executable(SMOKE)
    if not ok:
        return Result(FAIL, why, "Run `chmod +x %s && git update-index --chmod=+x %s`" % (SMOKE, SMOKE))
    if has_marker(ctx, SMOKE, STUB):
        return Result(FAIL, "%s is still the shipped stub (contains %s)" % (SMOKE, STUB))
    return Result(PASS, why)


@check("V2.smoke_pass", 2,
       "The smoke check passes right now (needs --run)",
       fix="Run `.harness/bin/smoke.sh`, fix what fails, then re-run the audit with --run",
       why="A smoke check that already fails cannot warn anyone when the agent breaks something.",
       how="Only with --run: executes .harness/bin/smoke.sh with a timeout; exit 0 is pass. Without --run it is not_measured.")
def smoke_pass(ctx) -> Result:
    if not ctx.run:
        return Result(NOT_MEASURED, "needs --run (executes %s)" % SMOKE, "Re-run the audit with --run")
    if not ctx.isfile(SMOKE):
        return Result(FAIL, "%s missing" % SMOKE)
    if has_marker(ctx, SMOKE, STUB):
        return Result(FAIL, "%s is the stub" % SMOKE)
    return run_repo_script(ctx, SMOKE)


CORE_EVIDENCE = ".harness/docs/evidence.md"


def _nonempty(ctx, rel: str) -> bool:
    return ctx.isfile(rel) and bool((ctx.read_text(rel) or "").strip())


@check("V2.evidence_rules", 2,
       "Rules that tell the agent to show proof (test output) before saying it is done",
       fix="Run the advance skill for rung 2 to install .harness/docs/evidence.md (and .claude/rules/evidence.md for Claude)",
       why="Without these rules the agent may say \"done\" without showing that anything was tested.",
       how="Checks the agent-neutral .harness/docs/evidence.md exists and is not empty; reports whether the Claude copy .claude/rules/evidence.md is present when claude is a configured agent.")
def evidence_rules(ctx) -> Result:
    if not _nonempty(ctx, CORE_EVIDENCE):
        return Result(FAIL, "%s missing or empty" % CORE_EVIDENCE)
    note = ""
    if "claude" in ctx.agents and not _nonempty(ctx, ".claude/rules/evidence.md"):
        note = "; Claude copy .claude/rules/evidence.md missing (Claude loads rules from there)"
    return Result(PASS, CORE_EVIDENCE + ctx.uncommitted(CORE_EVIDENCE) + note)


agent_check("V2.stop_hook_effective", 2,
            "The agent is stopped from finishing until it has proof (a Stop hook that really runs)",
            fix="Run the advance skill for rung 2; it registers the Stop hook in .claude/settings.json and installs its script under .harness/hooks/",
            why="Written rules can be ignored; a hook is checked by the tool itself every time the agent tries to stop.",
            how="Reads the committed .claude/settings.json, finds Stop command hooks, and checks the script each one runs exists inside the repo (and is executable when run directly).")


def _settings_or_fail(ctx):
    s, err = ctx.settings()
    if err == "missing":
        return None, Result(FAIL, ".claude/settings.json missing")
    if err:
        return None, Result(FAIL, ".claude/settings.json %s" % err,
                            "Fix the JSON syntax in .claude/settings.json")
    if s.get("disableAllHooks") is True:
        return None, Result(FAIL, ".claude/settings.json sets disableAllHooks: true",
                            "Remove disableAllHooks from .claude/settings.json")
    # Local settings take precedence on this machine, so a true there turns
    # every hook off here even when the committed file is fine.
    local, _ = ctx.load_json(LOCAL_SETTINGS)
    if isinstance(local, dict) and local.get("disableAllHooks") is True:
        return None, Result(FAIL, "%s sets disableAllHooks: true (no hook runs on this machine)" % LOCAL_SETTINGS,
                            "Remove disableAllHooks from %s" % LOCAL_SETTINGS)
    return s, None


LOCAL_SETTINGS = ".claude/settings.local.json"


def modified_guard(ctx, rel, deps=(".harness/bin/_lib.sh",)):
    """FAIL Result when a managed script is not the shipped one (see installed.guard_problem), else None."""
    for r in [rel] + [d for d in deps if ctx.isfile(d)]:
        why = installed.ctx_guard_problem(ctx, r)
        if why:
            return Result(FAIL, "%s %s" % (r, why),
                          "Re-run the installer, or review the change to %s and list it in "
                          "customized_scripts in .harness/config.json if it is approved" % r)
    return None


@evaluator("V2.stop_hook_effective", "claude")
def _stop_claude(ctx) -> Result:
    s, bad = _settings_or_fail(ctx)
    if bad:
        return bad
    handlers = list(claude_cfg.hook_handlers(s, "Stop"))
    if not handlers:
        return Result(FAIL, "no Stop hook registered in .claude/settings.json")
    notes = []
    other_types = 0
    for h in handlers:
        if h.get("type", "command") != "command":
            other_types += 1
            continue
        ok, why = claude_cfg.hook_effective(ctx, h)
        if ok:
            return Result(PASS, "Stop hook `%s`: %s" % (h.get("command"), why))
        notes.append(why)
    if other_types and not notes:
        return Result(UNKNOWN, "only prompt/agent Stop hooks; their effect cannot be checked statically")
    return Result(FAIL, "Stop hook not effective: %s" % "; ".join(notes[:3]))


agent_check("V2.verify_skill", 2,
            "The agent has a written procedure for checking its work (verify skill)",
            fix="Run the advance skill for rung 2 to install .claude/skills/verify/SKILL.md",
            why="Without a fixed checking procedure, each run of the agent checks its work differently, or not at all.",
            how="For Claude: checks .claude/skills/verify/SKILL.md exists and is not empty.")


@evaluator("V2.verify_skill", "claude")
def _verify_claude(ctx) -> Result:
    rel = ".claude/skills/verify/SKILL.md"
    if _nonempty(ctx, rel):
        return Result(PASS, rel + ctx.uncommitted(rel))
    return Result(FAIL, "%s missing or empty" % rel)


PR_TEMPLATES = (".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md",
                "pull_request_template.md", "PULL_REQUEST_TEMPLATE.md",
                "docs/pull_request_template.md", "docs/PULL_REQUEST_TEMPLATE.md")


def pr_templates(ctx) -> List[str]:
    found = [ctx.find_ci(p) for p in PR_TEMPLATES]
    out = [f for f in found if f]
    for d in (".github/PULL_REQUEST_TEMPLATE", ".github/pull_request_template"):
        out += [d + "/" + n for n in ctx.listdir(d) if n.endswith(".md")]
    return sorted(set(out))


@check("V2.pr_template_evidence", 2,
       "The pull request form asks for evidence",
       fix="Add an \"## Evidence\" section to .github/pull_request_template.md asking for the test and smoke output",
       why="Reviewers then have to trust the agent's word instead of seeing proof.",
       how="Looks for a PR template (.github/pull_request_template.md and the other GitHub locations) with a heading or bold label containing \"Evidence\".")
def pr_template_evidence(ctx) -> Result:
    tpls = pr_templates(ctx)
    if not tpls:
        return Result(FAIL, "no pull request template")
    for t in tpls:
        for i, l in enumerate(ctx.lines(t), 1):
            if re.match(r"(?i)^\s*(#+\s.*evidence|\*\*[^*]*evidence[^*]*\*\*)", l):
                return Result(PASS, "%s:%d" % (t, i))
    return Result(FAIL, "%s has no Evidence section" % ", ".join(tpls))


# Linter and type-checker configs, adapted from the agent-readiness scanner
# (MIT, lint.configured / typecheck.configured).
LINT_FILES = {"ruff.toml", ".ruff.toml", ".flake8", ".pylintrc", "pylintrc", ".eslintrc",
              ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml",
              "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
              "biome.json", "biome.jsonc", ".golangci.yml", ".golangci.yaml", ".golangci.toml",
              "clippy.toml", ".clippy.toml", ".rubocop.yml", ".standard.yml", "phpcs.xml",
              "phpcs.xml.dist", ".credo.exs", "detekt.yml", ".swiftlint.yml", ".shellcheckrc",
              "oxlint.json", ".oxlintrc.json", "deno.json"}
TYPE_FILES = {"mypy.ini", ".mypy.ini", "pyrightconfig.json", ".pyre_configuration", "tsconfig.json",
              "jsconfig.json", "phpstan.neon", "phpstan.neon.dist", "psalm.xml", "psalm.xml.dist",
              "sorbet/config", "Steepfile"}
TYPED_ECO = {"go", "rust", "java", "kotlin"}


@check("V2.lint_configured", 2,
       "A linter (automatic style and mistake checker) is set up",
       fix="Add a linter config for your language (for example ruff.toml, eslint.config.js or .golangci.yml) and a lint command",
       why="A linter catches many small mistakes before a human has to.",
       how="Looks for common linter config files, [tool.ruff]/[tool.pylint] in pyproject.toml, eslintConfig in package.json, or a detected lint command.",
       severity=RECOMMENDED)
def lint_configured(ctx) -> Result:
    hits = [f for f in LINT_FILES if ctx.isfile(f)]
    py = ctx.read_text("pyproject.toml") or ""
    if re.search(r"\[tool\.(ruff|pylint|flake8)", py):
        hits.append("pyproject.toml")
    if re.search(r'"eslintConfig"\s*:', ctx.read_text("package.json") or ""):
        hits.append("package.json eslintConfig")
    if hits:
        return Result(PASS, ", ".join(sorted(hits)[:4]))
    if ctx.detected["commands"].get("lint"):
        return Result(PASS, "lint command `%s`" % ctx.detected["commands"]["lint"])
    return Result(FAIL, "no linter config found")


@check("V2.types_configured", 2,
       "A type checker is set up (or the language checks types itself)",
       fix="Add a type checker config (for example tsconfig.json with \"strict\": true, or mypy.ini)",
       why="Type checks catch wrong arguments and missing fields that tests often miss.",
       how="Looks for tsconfig.json, mypy/pyright config, PHPStan/Psalm, Sorbet/Steep, or a statically typed language (Go, Rust, Java, Kotlin).",
       severity=RECOMMENDED)
def types_configured(ctx) -> Result:
    hits = [f for f in TYPE_FILES if ctx.isfile(f)]
    if re.search(r"\[tool\.(mypy|pyright|pyre)\]", ctx.read_text("pyproject.toml") or ""):
        hits.append("pyproject.toml")
    typed = sorted(TYPED_ECO & set(ctx.detected["ecosystems"]))
    if hits or typed:
        return Result(PASS, ", ".join(sorted(hits)[:4] + ["%s is statically typed" % t for t in typed]))
    return Result(FAIL, "no type checker config found")


def ci_runs_gates_result(ctx) -> Result:
    wfs = load_workflows(ctx)
    if not wfs:
        return Result(FAIL, "no GitHub Actions workflows in .github/workflows/")
    ignored = ""
    for w in wfs:
        if is_agent_text(w.text):
            continue  # an agent being allowed to run the gates is not CI running them
        for i, l in enumerate(w.text.splitlines(), 1):
            if GATES in l and "Bash(" not in l:
                why = neutered_reason(w.text, (GATES,))
                if why:
                    ignored = "%s: %s" % (w.path, why)
                    break
                return Result(PASS, "%s:%d runs %s" % (w.path, i, GATES))
    return Result(FAIL, ignored or "no CI workflow runs %s" % GATES)


CI_GATES_FIX = "Add a step `run: .harness/bin/gates.sh` to a workflow in .github/workflows/ that runs on pull_request"
CI_GATES_WHY = "If CI does not run the same gates, a change that fails them can still be merged."
CI_GATES_HOW = "Searches .github/workflows/*.yml (comments ignored, agent workflows skipped) for a line that runs .harness/bin/gates.sh. Fails if that job is disabled, sets continue-on-error, or ignores the result (|| true)."


@check("V2.ci_runs_gates", 2, "CI runs the same gates on every pull request",
       fix=CI_GATES_FIX, why=CI_GATES_WHY, how=CI_GATES_HOW, severity=RECOMMENDED)
def ci_runs_gates_v2(ctx) -> Result:
    return ci_runs_gates_result(ctx)
