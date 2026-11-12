"""OpenAI Codex CLI evaluators.

Formats (developers.openai.com/codex/hooks.md, rules.md, config-basic.md):
  .codex/hooks.json  {"hooks": {"PreToolUse"|"Stop": [{"matcher": "...",
                      "hooks": [{"type": "command", "command": "..."}]}]}}
  .codex/rules/harness.rules  Starlark: prefix_rule(pattern=["gh", "pr", "merge"],
                      decision="forbidden", justification="...")
Project hooks and rules load only when the project's .codex/ layer is trusted,
and hooks must be reviewed in /hooks before they run. Hook crashes fail open.
"""
from __future__ import annotations

import ast
import re
import shlex
from typing import List, Optional, Tuple

from .registry import FAIL, PASS, Result, evaluator
from .agents import AgentSpec, register_agent
from .agent_common import (ACCEPTANCE, CI_ENFORCES, judge_hooks, nested_entries,
                           register_common, register_na)

SPEC = register_agent(AgentSpec(
    id="codex", name="Codex CLI",
    reads_agents_md="yes, natively",
    edit_guard="PreToolUse hook in `.codex/hooks.json`",
    stop_gate="Stop hook in `.codex/hooks.json`",
    deny_rules="`.codex/rules/harness.rules` (`prefix_rule(..., decision=\"forbidden\")`, commands only)",
    fail_mode="fails open (the tool call continues)",
    user_once="trust the project, then review and trust the hooks in `/hooks`",
    footprints=(".codex/",),
    hook_file=".codex/hooks.json", pretool_event="PreToolUse", stop_event="Stop",
    deny_file=".codex/rules/harness.rules", verify_skill=".agents/skills/verify/SKILL.md",
    docs="developers.openai.com/codex/hooks.md, rules.md, config-basic.md",
))
TRUST = ("loads only when the project is trusted and the hooks are reviewed in /hooks; "
         "Codex hooks fail open on a crash, so CI is the real stop")
RULES_TRUST = "rules load only when the project is trusted (rules are experimental)"

register_common(SPEC)


def _hooks(ctx) -> Tuple[Optional[dict], Optional[Result]]:
    f = SPEC.hook_file
    data, err = ctx.load_json(f)
    if err == "missing":
        return None, Result(FAIL, "%s missing" % f,
                            "Run the advance skill for codex; it writes %s" % f)
    if err or not isinstance(data, dict):
        return None, Result(FAIL, "%s %s" % (f, err or "top level is not an object"),
                            "Fix the JSON in %s" % f)
    cfg = ctx.read_text(".codex/config.toml") or ""
    if re.search(r"(?m)^\s*(codex_)?hooks\s*=\s*false\b", cfg):
        return None, Result(FAIL, ".codex/config.toml turns hooks off (hooks = false)",
                            "Remove `hooks = false` from .codex/config.toml")
    return data, None


def _judge(ctx, event: str) -> Result:
    data, bad = _hooks(ctx)
    if bad:
        return bad
    return judge_hooks(ctx, SPEC, event, nested_entries(data, event, SPEC.hook_file), TRUST)


@evaluator("V2.stop_hook_effective", "codex",
           how="%s has a Stop command hook whose script (after stripping the "
               "`$(git rev-parse --show-toplevel)` wrapper and `--agent ID`) exists under .harness/hooks/; "
               "notes that the project must be trusted and the hooks reviewed." % SPEC.hook_file)
def _stop(ctx) -> Result:
    return _judge(ctx, SPEC.stop_event)


@evaluator("G3.pretool_guard_effective", "codex",
           how="%s has a PreToolUse command hook whose script exists under .harness/hooks/; "
               "passes with a note that Codex hooks fail open on a crash." % SPEC.hook_file)
def _pretool(ctx) -> Result:
    return _judge(ctx, SPEC.pretool_event)


# ---------------------------------------------------------------- rules
def _call_bodies(text: str, name: str) -> List[str]:
    out, i = [], 0
    while True:
        m = re.compile(r"\b%s\s*\(" % name).search(text, i)
        if not m:
            return out
        depth, j, q = 1, m.end(), None
        while j < len(text) and depth:
            c = text[j]
            if q:
                if c == "\\":
                    j += 1
                elif c == q:
                    q = None
            elif c in "\"'":
                q = c
            elif c in "([":
                depth += 1
            elif c in ")]":
                depth -= 1
            j += 1
        out.append(text[m.end():j - 1])
        i = j


def _kwarg(body: str, key: str) -> Optional[object]:
    m = re.search(r"\b%s\s*=\s*" % key, body)
    if not m:
        return None
    rest = body[m.end():]
    for end in range(1, len(rest) + 1):  # shortest prefix that parses as a literal
        if rest[end - 1] not in ")],\"'":
            continue
        try:
            return ast.literal_eval(rest[:end].strip())
        except (ValueError, SyntaxError):
            continue
    return None


def forbidden_patterns(ctx) -> List[list]:
    text = ctx.read_text(SPEC.deny_file) or ""
    text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    out = []
    for body in _call_bodies(text, "prefix_rule"):
        pattern, decision = _kwarg(body, "pattern"), _kwarg(body, "decision")
        if decision == "forbidden" and isinstance(pattern, list) and pattern:
            out.append(pattern)
    return out


def pattern_matches(pattern: list, command: str) -> bool:
    toks = shlex.split(command)
    if len(toks) < len(pattern):
        return False
    for el, tok in zip(pattern, toks):
        alts = el if isinstance(el, list) else [el]
        if tok not in alts:
            return False
    return True


@evaluator("G3.deny_rules", "codex",
           how="%s has prefix_rule(..., decision=\"forbidden\") rules matching `gh pr merge 1` and "
               "`git push origin <default branch>` (Codex prefix matching). Codex rules cover commands "
               "only, so .harness and AGENTS.md edits rely on the guard hook and CI." % SPEC.deny_file)
def _deny(ctx) -> Result:
    f = SPEC.deny_file
    fix = "Add prefix_rule(pattern=[\"gh\", \"pr\", \"merge\"], decision=\"forbidden\") and a forbidden " \
          "`git push` rule to %s" % f
    if not ctx.isfile(f):
        return Result(FAIL, "%s missing" % f, fix)
    pats = forbidden_patterns(ctx)
    branch = ctx.default_branch or "main"
    required = ["gh pr merge 1", "git push origin %s" % branch]
    extra = ["git push origin HEAD:%s" % branch, "git push -f origin %s" % branch]
    missing = [c for c in required if not any(pattern_matches(p, c) for p in pats)]
    if missing:
        return Result(FAIL, "not forbidden in %s: %s (%d forbidden rules)"
                      % (f, ", ".join("`%s`" % c for c in missing), len(pats)), fix)
    soft = [c for c in extra if not any(pattern_matches(p, c) for p in pats)]
    ev = "%s forbids %s; %s; edits to .harness and AGENTS.md are not covered by Codex rules " \
         "(the guard hook and CI cover them)" % (f, ", ".join("`%s`" % c for c in required), RULES_TRUST)
    if soft:
        ev += "; also consider forbidding %s" % ", ".join("`%s`" % c for c in soft)
    return Result(PASS, ev)


register_na("G3.acceptance_protected:deny", "codex",
            "Codex rules cover commands, not file edits; the guard hook and CI cover %s" % ACCEPTANCE,
            "Codex has no repo-level edit deny rule (its rules cover commands only); "
            "no in-session deny for %s; the PreToolUse guard (fails open) and %s" % (ACCEPTANCE, CI_ENFORCES))
