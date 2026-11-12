"""Rung 3, Guarded: the agent cannot change what it must not, or merge alone."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from .registry import (FAIL, NA, NOT_MEASURED, PASS, RECOMMENDED, UNKNOWN, Result,
                       agent_check, check, evaluator)
from .context import has_marker
from . import claude_cfg
from .rung2 import (CI_GATES_FIX, CI_GATES_HOW, CI_GATES_WHY, STUB, _settings_or_fail,
                    ci_runs_gates_result, modified_guard, run_repo_script)
from .workflows import load_workflows, neutered_reason, triggers_on_pull_request

PROTECTED = ".harness/protected-paths.txt"
DIFF_GUARD = ".harness/bin/diff-guard.sh"
WF_LINT = ".harness/bin/lint-agent-workflows.py"
ACCEPTANCE = ".harness/acceptance"
GUARD_LIB_NAME = "harness_guard.py"
GUARD_LIB = ".harness/bin/" + GUARD_LIB_NAME
# Hook `onFailure` (fail closed) per the Claude Code hooks reference.
ON_FAILURE_MIN = "v2.1.295"


@check("G3.protected_paths", 3,
       "A list of files the agent must never change (.harness/protected-paths.txt)",
       fix="Create .harness/protected-paths.txt with one path or glob per line (start with .harness/ and .github/workflows/)",
       why="Without this list, the agent can edit the tests or checks that are supposed to judge it.",
       how="Checks .harness/protected-paths.txt has at least one line that is not blank or a comment.")
def protected_paths(ctx) -> Result:
    if not ctx.isfile(PROTECTED):
        return Result(FAIL, "%s missing" % PROTECTED)
    lines = ctx.meaningful_lines(PROTECTED)
    if not lines:
        return Result(FAIL, "%s is empty" % PROTECTED)
    return Result(PASS, "%d protected entries%s" % (len(lines), ctx.uncommitted(PROTECTED)))


@check("G3.diff_guard", 3,
       "A script that rejects changes to protected files or changes that are too big (.harness/bin/diff-guard.sh)",
       fix="Run the advance skill for rung 3 to install .harness/bin/diff-guard.sh",
       why="Without it, a change that rewrites protected files or thousands of lines looks like any other change.",
       how="Checks .harness/bin/diff-guard.sh exists, is committed as executable, has no HARNESS_STUB marker, and that it and .harness/bin/harness_guard.py (which it runs) match the shipped scripts rendered with .harness/config.json, unless listed in config customized_scripts.")
def diff_guard(ctx) -> Result:
    if not ctx.isfile(DIFF_GUARD):
        return Result(FAIL, "%s missing" % DIFF_GUARD)
    ok, why = ctx.executable(DIFF_GUARD)
    if not ok:
        return Result(FAIL, why, "Run `chmod +x %s && git update-index --chmod=+x %s`" % (DIFF_GUARD, DIFF_GUARD))
    if has_marker(ctx, DIFF_GUARD, STUB):
        return Result(FAIL, "%s is a stub (contains %s)" % (DIFF_GUARD, STUB))
    # diff-guard.sh only execs harness_guard.py, so the real logic is there.
    if not ctx.isfile(GUARD_LIB):
        return Result(FAIL, "%s missing (%s runs it)" % (GUARD_LIB, DIFF_GUARD))
    bad = modified_guard(ctx, DIFF_GUARD, deps=(GUARD_LIB, ".harness/bin/_lib.sh"))
    if bad:
        return bad
    return Result(PASS, why)


agent_check("G3.pretool_guard_effective", 3,
            "A guard runs before every agent action: protected paths need a human's approval, forbidden actions are blocked, a crash blocks",
            fix="Run the advance skill for rung 3; it registers a PreToolUse hook with \"onFailure\": \"block\" in .claude/settings.json",
            why="Without a guard that fails closed, a crash or timeout quietly lets the forbidden action through.",
            how="For Claude: reads .claude/settings.json (and fails if it or .claude/settings.local.json sets disableAllHooks) and needs a PreToolUse command hook with \"onFailure\": \"block\" whose script exists in the repo (and is executable when run directly) and matches the shipped script rendered with .harness/config.json (unless listed in config customized_scripts). onFailure requires Claude Code %s or later; older versions ignore it and let the action through when the hook fails. The shipped guard answers \"ask\" for protected paths and exits 2 for .harness/acceptance/." % ON_FAILURE_MIN)


@evaluator("G3.pretool_guard_effective", "claude")
def _pretool_claude(ctx) -> Result:
    s, bad = _settings_or_fail(ctx)
    if bad:
        return bad
    handlers = [h for h in claude_cfg.hook_handlers(s, "PreToolUse") if h.get("type", "command") == "command"]
    if not handlers:
        return Result(FAIL, "no PreToolUse command hook in .claude/settings.json")
    blocking = [h for h in handlers if h.get("onFailure") == "block"]
    if not blocking:
        return Result(FAIL, "%d PreToolUse hook(s) but none has \"onFailure\": \"block\"" % len(handlers),
                      "Add \"onFailure\": \"block\" to the guard hook in .claude/settings.json "
                      "(honored by Claude Code %s or later)" % ON_FAILURE_MIN)
    # A guard replaced by `exit 0` still exists and is executable; only the
    # install-time hash shows it no longer guards anything.
    for h in blocking:
        rel, _, _ = claude_cfg.resolve_hook_target(ctx, h)
        if not rel or not ctx.isfile(rel):
            continue
        for target in [rel] + ([GUARD_LIB] if GUARD_LIB_NAME in (ctx.read_text(rel) or "") else []):
            bad = modified_guard(ctx, target)
            if bad:
                return bad
    notes = []
    for h in blocking:
        ok, why = claude_cfg.hook_effective(ctx, h)
        if ok:
            return Result(PASS, "PreToolUse `%s` (onFailure block): %s; onFailure needs Claude Code %s or later"
                          % (h.get("command"), why, ON_FAILURE_MIN))
        notes.append(why)
    return Result(FAIL, "PreToolUse guard not effective: %s" % "; ".join(notes[:3]))


agent_check("G3.deny_rules", 3,
            "The agent is forbidden to merge pull requests or push to the main branch, and harness edits need approval",
            fix="Add \"Bash(gh pr merge *)\" and \"Bash(git push * <default branch>*)\" style rules to permissions.deny and \"Edit(/.harness/**)\", \"Edit(AGENTS.md)\" to permissions.ask (or deny) in .claude/settings.json (the rung 3 template does this)",
            why="Without these rules, nothing stops the agent from shipping its own change without review.",
            how="For Claude: tests permissions.deny in .claude/settings.json against sample commands `gh pr merge 1` and `git push origin <default branch>` using Claude Code's rule matching, and needs an ask or deny Edit rule naming .harness and AGENTS.md (ask rules prompt in every permission mode and are denied when no one can answer).")


@evaluator("G3.deny_rules", "claude")
def _deny_claude(ctx) -> Result:
    s, bad = _settings_or_fail(ctx)
    if bad:
        return bad
    rules = claude_cfg.deny_rules(s)
    branch = ctx.default_branch or "main"
    required = ["gh pr merge 1", "git push origin %s" % branch]
    extra = ["gh pr merge --admin --squash 1", "git push origin HEAD:%s" % branch,
             "git push -f origin %s" % branch]
    missing = [c for c in required if not claude_cfg.denies_command(rules, c)]
    soft = [c for c in extra if not claude_cfg.denies_command(rules, c)]
    if missing:
        return Result(FAIL, "not denied: %s (%d deny rules)" % (", ".join("`%s`" % c for c in missing), len(rules)))
    gated = rules + claude_cfg.deny_rules(s, "ask")
    ungated = [p for p in (".harness", "AGENTS.md") if not claude_cfg.denies_path_edit(gated, p)]
    if ungated:
        return Result(FAIL, "no ask or deny Edit rule for %s" % ", ".join(ungated),
                      "Add \"Edit(/.harness/**)\" and \"Edit(AGENTS.md)\" to permissions.ask in .claude/settings.json")
    ev = "denies %s; edits to .harness and AGENTS.md need approval" % ", ".join("`%s`" % c for c in required)
    if soft:
        ev += "; also consider denying %s" % ", ".join("`%s`" % c for c in soft)
    return Result(PASS, ev)


@check("G3.acceptance_protected", 3,
       "The acceptance tests are locked against agent edits (.harness/acceptance/)",
       fix="Add .harness/acceptance/ to .harness/protected-paths.txt and \"Edit(/.harness/acceptance/**)\" to permissions.deny in .claude/settings.json",
       why="If the agent can edit the tests that judge it, it can make them pass without doing the work.",
       how="Checks .harness/protected-paths.txt lists .harness/acceptance and, for each configured agent, that its deny rules cover edits there (Claude: an Edit/Write deny rule naming .harness/acceptance; an ask rule does not count, since a person at the prompt could approve it).")
def acceptance_protected(ctx) -> Result:
    listed = any(ACCEPTANCE in l for _, l in ctx.meaningful_lines(PROTECTED))
    if not listed:
        return Result(FAIL, "%s not listed in %s" % (ACCEPTANCE + "/", PROTECTED))
    from .registry import evaluate_per_agent
    r = evaluate_per_agent(ctx, "G3.acceptance_protected:deny")
    if r.status not in (PASS, NA):  # n/a: an agent with no deny file (CI and branch protection enforce)
        return r
    return Result(PASS, "listed in %s; %s" % (PROTECTED, r.evidence))


@evaluator("G3.acceptance_protected:deny", "claude")
def _acceptance_claude(ctx) -> Result:
    s, bad = _settings_or_fail(ctx)
    if bad:
        return bad
    hits = claude_cfg.denies_path_edit(claude_cfg.deny_rules(s), ACCEPTANCE)
    if not hits:
        return Result(FAIL, "no Edit/Write deny rule for %s in .claude/settings.json" % ACCEPTANCE,
                      "Add \"Edit(/.harness/acceptance/**)\" to permissions.deny in .claude/settings.json")
    return Result(PASS, "deny %s" % ", ".join(hits[:2]))


@check("G3.ci_guard", 3,
       "CI runs the gates, the diff guard and the workflow checker on every pull request",
       fix="Run the advance skill for rung 3 to install the guard workflow in .github/workflows/ (it runs gates.sh, diff-guard.sh and lint-agent-workflows.py)",
       why="Local guards can be skipped; CI is the place every change has to pass through.",
       how="Looks for one workflow that triggers on pull_request and references gates.sh, diff-guard.sh and lint-agent-workflows.py. Fails if that job is disabled (if: false), sets continue-on-error, or ignores a script's result (|| true).")
def ci_guard(ctx) -> Result:
    wfs = load_workflows(ctx)
    if not wfs:
        return Result(FAIL, "no workflows in .github/workflows/")
    # The shipped guard workflow copies the scripts to $RUNNER_TEMP first, so
    # match on script names, not full .harness/bin paths.
    needles = ("gates.sh", "diff-guard.sh", "lint-agent-workflows.py")
    best, best_have = None, []
    for w in wfs:
        have = [n for n in needles if n in w.text]
        if triggers_on_pull_request(w.text) and len(have) == 3:
            why = neutered_reason(w.text, needles)
            if why:
                return Result(FAIL, "%s: %s" % (w.path, why))
            return Result(PASS, "%s runs gates, diff guard and workflow lint" % w.path)
        if len(have) > len(best_have):
            best, best_have = w, have
    if best is None:
        return Result(FAIL, "no workflow references any of %s" % ", ".join(needles))
    missing = [n for n in needles if n not in best_have]
    trig = "" if triggers_on_pull_request(best.text) else "; not triggered on pull_request"
    if not missing:
        return Result(FAIL, "%s is not triggered on pull_request" % best.path)
    return Result(FAIL, "%s is missing %s%s" % (best.path, ", ".join(missing), trig))


@check("G3.ci_runs_gates", 3, "CI runs the same gates on every pull request",
       fix=CI_GATES_FIX, why=CI_GATES_WHY, how=CI_GATES_HOW)
def ci_runs_gates_g3(ctx) -> Result:
    return ci_runs_gates_result(ctx)


@check("G3.workflow_lint_clean", 3,
       "The agent workflows pass the safety checker (needs --run)",
       fix="Run `python3 .harness/bin/lint-agent-workflows.py`, fix each finding in .github/workflows/, then re-run the audit with --run",
       why="An unsafe workflow can let text from a stranger's issue control the agent or leak secrets.",
       how="Only with --run: executes .harness/bin/lint-agent-workflows.py with python3; exit 0 is pass. Without --run it is not_measured.")
def workflow_lint_clean(ctx) -> Result:
    if not ctx.isfile(WF_LINT):
        return Result(FAIL, "%s missing" % WF_LINT,
                      "Run the advance skill for rung 3 to install %s" % WF_LINT)
    if not ctx.run:
        return Result(NOT_MEASURED, "needs --run (executes %s)" % WF_LINT, "Re-run the audit with --run")
    return run_repo_script(ctx, WF_LINT, interpreter=sys.executable or "python3", timeout=120)


def github_repo(ctx):
    r = ctx.git("config", "--get-regexp", r"^remote\..*\.url$")
    if not r or r[0] != 0:
        return None
    urls = {}
    for line in r[1].splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            urls[parts[0].split(".")[1]] = parts[1].strip()
    for name in ["origin"] + sorted(urls):
        u = urls.get(name)
        if not u:
            continue
        m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", u)
        if m:
            return m.group(1), m.group(2)
    return None


@check("G3.branch_protection", 3,
       "GitHub blocks direct pushes to the main branch (branch protection)",
       fix="On GitHub open Settings > Branches (or Rules) and protect the default branch: require a pull request and the gates check",
       why="Repo settings files can be changed by the agent; GitHub's own protection is the lock it cannot pick.",
       how="Makes the audit's only network call: `gh api repos/OWNER/REPO/branches/BRANCH` and reads `protected`. Unknown when --offline, gh is missing or not logged in, or there is no GitHub remote.")
def branch_protection(ctx) -> Result:
    if ctx.offline:
        return Result(UNKNOWN, "skipped (--offline)", "Re-run without --offline, or check Settings > Branches on GitHub")
    if not shutil.which("gh"):
        return Result(UNKNOWN, "gh CLI not on PATH", "Install the GitHub CLI and run `gh auth login`, then re-run the audit")
    repo = github_repo(ctx) if ctx.is_git else None
    if not repo:
        return Result(UNKNOWN, "no GitHub remote found", "Add a GitHub remote (`git remote add origin ...`) and re-run")
    branch = ctx.default_branch or "main"
    env = dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1")
    try:
        p = subprocess.run(["gh", "api", "repos/%s/%s/branches/%s" % (repo[0], repo[1], branch)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=20, env=env, cwd=ctx.root)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Result(UNKNOWN, "gh api failed: %s" % e.__class__.__name__)
    if p.returncode != 0:
        msg = (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["error"])[-1][:160]
        return Result(UNKNOWN, "gh api failed: %s" % msg, "Run `gh auth login` and check the repo and branch name, then re-run")
    try:
        data = json.loads(p.stdout.decode("utf-8", "replace"))
    except ValueError:
        return Result(UNKNOWN, "gh api returned non-JSON output")
    if data.get("protected") is True:
        return Result(PASS, "%s/%s branch %s is protected" % (repo[0], repo[1], branch))
    if data.get("protected") is False:
        return Result(FAIL, "%s/%s branch %s is not protected (as reported by the branches API)"
                      % (repo[0], repo[1], branch))
    return Result(UNKNOWN, "branches API response has no `protected` field")


@check("G3.codeowners", 3,
       "Named owners must review changes to sensitive files (CODEOWNERS)",
       fix="Create .github/CODEOWNERS with lines like `.harness/ @your-team` and `.github/ @your-team`",
       why="Without owners, a change to the harness itself can be approved by anyone.",
       how="Looks for a non-empty CODEOWNERS in the repo root, .github/ or docs/.",
       severity=RECOMMENDED)
def codeowners(ctx) -> Result:
    for rel in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        if ctx.meaningful_lines(rel):
            return Result(PASS, rel)
    return Result(FAIL, "no CODEOWNERS file")


@check("G3.secret_scanning", 3,
       "A secret scanner runs automatically (gitleaks or similar)",
       fix="Add a gitleaks step to CI or to .pre-commit-config.yaml, for example the gitleaks/gitleaks-action workflow step",
       why="The basic scan here only knows a few key formats; a real scanner catches far more before they are pushed.",
       how="Looks for .gitleaks.toml, or gitleaks, trufflehog or detect-secrets in .pre-commit-config.yaml or any workflow.",
       severity=RECOMMENDED)
def secret_scanning(ctx) -> Result:
    if ctx.isfile(".gitleaks.toml"):
        return Result(PASS, ".gitleaks.toml")
    tools = re.compile(r"gitleaks|trufflehog|detect-secrets|ggshield")
    pc = ctx.read_text(".pre-commit-config.yaml") or ""
    if tools.search(pc):
        return Result(PASS, ".pre-commit-config.yaml uses %s" % tools.search(pc).group(0))
    for w in load_workflows(ctx):
        m = tools.search(w.text)
        if m:
            return Result(PASS, "%s uses %s" % (w.path, m.group(0)))
    return Result(FAIL, "no secret scanner configured (GitHub's built-in scanning is not checked)")


agent_check("G3.sandbox_settings", 3,
            "Agent commands run in a sandbox that refuses to run unsandboxed",
            fix="Set \"sandbox\": {\"enabled\": true, \"failIfUnavailable\": true} in .claude/settings.json",
            why="Without a sandbox, one bad command from the agent can touch anything on the machine.",
            how="For Claude: checks .claude/settings.json has sandbox.enabled true and sandbox.failIfUnavailable true (otherwise it silently runs unsandboxed where the sandbox is unsupported).",
            severity=RECOMMENDED)


@evaluator("G3.sandbox_settings", "claude")
def _sandbox_claude(ctx) -> Result:
    s, bad = _settings_or_fail(ctx)
    if bad:
        return bad
    sb = s.get("sandbox") if isinstance(s.get("sandbox"), dict) else {}
    if sb.get("enabled") is not True:
        return Result(FAIL, "sandbox.enabled is not true in .claude/settings.json")
    if sb.get("failIfUnavailable") is not True:
        return Result(FAIL, "sandbox enabled but failIfUnavailable is not true (falls back to no sandbox)",
                      "Set sandbox.failIfUnavailable to true in .claude/settings.json")
    return Result(PASS, "sandbox enabled, failIfUnavailable true")
