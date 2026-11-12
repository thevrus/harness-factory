"""Cursor evaluators (editor agent, CLI `agent`, cloud agents).

Formats (cursor.com/docs/hooks.md, cli/reference/permissions.md):
  .cursor/hooks.json  {"version": 1, "hooks": {"preToolUse"|"stop": [{"command": "...",
                      "type": "command", "matcher": "...", "failClosed": true}]}}
                      Project hook commands run from the project root.
  .cursor/cli.json    {"permissions": {"deny": ["Shell(gh:pr merge*)", "Write(.harness/acceptance/**)"]}}
Crashes, timeouts and non-zero exits other than 2 fail open unless failClosed is true.
"""
from __future__ import annotations

import fnmatch
import shlex
from typing import List, Optional, Tuple

from .registry import FAIL, PASS, Result, evaluator
from .agents import AgentSpec, register_agent
from .agent_common import ACCEPTANCE, HookEntry, flat_list, judge_hooks, register_common
from .claude_cfg import deny_rules, parse_rule

SPEC = register_agent(AgentSpec(
    id="cursor", name="Cursor",
    reads_agents_md="yes, natively",
    edit_guard="preToolUse hook in `.cursor/hooks.json`",
    stop_gate="stop hook in `.cursor/hooks.json` (followup_message, loop_limit)",
    deny_rules="`.cursor/cli.json` `permissions.deny` (CLI only; editor allowlists are UI settings)",
    fail_mode="fails open unless the hook sets `\"failClosed\": true`",
    user_once="nothing for the CLI in a trusted workspace (`--trust` for headless runs)",
    footprints=(".cursor/", ".cursorrules"),
    hook_file=".cursor/hooks.json", pretool_event="preToolUse", stop_event="stop",
    deny_file=".cursor/cli.json", verify_skill=".agents/skills/verify/SKILL.md",
    docs="cursor.com/docs/hooks.md, cursor.com/docs/cli/reference/permissions.md",
))

register_common(SPEC)


def _load(ctx, rel: str) -> Tuple[Optional[dict], Optional[Result]]:
    data, err = ctx.load_json(rel)
    if err == "missing":
        return None, Result(FAIL, "%s missing" % rel, "Run the advance skill for cursor; it writes %s" % rel)
    if err or not isinstance(data, dict):
        return None, Result(FAIL, "%s %s" % (rel, err or "top level is not an object"),
                            "Fix the JSON in %s" % rel)
    return data, None


def _entries(data: dict, event: str) -> List[HookEntry]:
    return [HookEntry(SPEC.hook_file, h, h.get("command"))
            for h in flat_list(data, event) if h.get("type", "command") == "command"]


@evaluator("V2.stop_hook_effective", "cursor",
           how="%s has a `stop` command hook whose script exists under .harness/hooks/ "
               "(commands resolve from the project root)." % SPEC.hook_file)
def _stop(ctx) -> Result:
    data, bad = _load(ctx, SPEC.hook_file)
    if bad:
        return bad
    return judge_hooks(ctx, SPEC, SPEC.stop_event, _entries(data, SPEC.stop_event),
                       "the stop hook continues the agent with followup_message (loop_limit caps it)")


def _fail_closed(e: HookEntry) -> Optional[str]:
    if e.handler.get("failClosed") is True:
        return None
    return "preToolUse hook `%s` lacks \"failClosed\": true (a crash would let the action through)" % e.command


@evaluator("G3.pretool_guard_effective", "cursor",
           how="%s has a `preToolUse` command hook with \"failClosed\": true whose script exists "
               "under .harness/hooks/." % SPEC.hook_file)
def _pretool(ctx) -> Result:
    data, bad = _load(ctx, SPEC.hook_file)
    if bad:
        return bad
    return judge_hooks(ctx, SPEC, SPEC.pretool_event, _entries(data, SPEC.pretool_event),
                       "failClosed true, so a crash or timeout blocks; Cursor does not enforce an "
                       "\"ask\" answer from preToolUse, so protected paths are denied, not asked",
                       fix="Set \"failClosed\": true on the preToolUse guard in %s" % SPEC.hook_file,
                       require=_fail_closed)


# ---------------------------------------------------------------- permissions
def shell_rule_matches(spec: Optional[str], command: str) -> bool:
    """Shell(base) matches the first token; Shell(base:args) also globs the arguments."""
    toks = shlex.split(command)
    if not toks:
        return False
    if spec is None or not spec.strip():
        return True
    base, sep, args = spec.strip().partition(":")
    if not fnmatch.fnmatchcase(toks[0], base):
        return False
    return not sep or fnmatch.fnmatchcase(" ".join(toks[1:]), args)


def denies(rules: List[str], command: str) -> Optional[str]:
    for r in rules:
        tool, spec = parse_rule(r)
        if tool == "Shell" and shell_rule_matches(spec, command):
            return r
    return None


def write_denies(rules: List[str], fragment: str) -> List[str]:
    hits = []
    for r in rules:
        tool, spec = parse_rule(r)
        if tool == "Write" and (spec is None or fragment.strip("/") in spec):
            hits.append(r)
    return hits


@evaluator("G3.deny_rules", "cursor",
           how="%s permissions.deny has Shell rules matching `gh pr merge 1` and `git push origin "
               "<default branch>` (Shell(base) or Shell(base:args) globs); notes Write rules for "
               ".harness and AGENTS.md. Applies to the Cursor CLI." % SPEC.deny_file)
def _deny(ctx) -> Result:
    data, bad = _load(ctx, SPEC.deny_file)
    if bad:
        return bad
    rules = deny_rules(data)
    branch = ctx.default_branch or "main"
    required = ["gh pr merge 1", "git push origin %s" % branch]
    missing = [c for c in required if not denies(rules, c)]
    if missing:
        return Result(FAIL, "not denied in %s: %s (%d deny rules)"
                      % (SPEC.deny_file, ", ".join("`%s`" % c for c in missing), len(rules)),
                      "Add \"Shell(gh:pr merge*)\" and \"Shell(git:push*)\" to permissions.deny in %s"
                      % SPEC.deny_file)
    ev = "%s denies %s (Cursor CLI)" % (SPEC.deny_file, ", ".join("`%s`" % c for c in required))
    ungated = [p for p in (".harness", "AGENTS.md") if not write_denies(rules, p)]
    if ungated:
        ev += "; no Write deny for %s (Cursor has no ask tier here; the guard hook covers it)" % ", ".join(ungated)
    return Result(PASS, ev)


@evaluator("G3.acceptance_protected:deny", "cursor",
           how="%s permissions.deny has a Write rule naming %s." % (SPEC.deny_file, ACCEPTANCE))
def _acceptance(ctx) -> Result:
    data, bad = _load(ctx, SPEC.deny_file)
    if bad:
        return bad
    hits = write_denies(deny_rules(data), ACCEPTANCE)
    if not hits:
        return Result(FAIL, "no Write deny rule for %s in %s" % (ACCEPTANCE, SPEC.deny_file),
                      "Add \"Write(%s/**)\" to permissions.deny in %s" % (ACCEPTANCE, SPEC.deny_file))
    return Result(PASS, "deny %s in %s" % (", ".join(hits[:2]), SPEC.deny_file))
