"""GitHub Copilot evaluators (cloud agent and Copilot CLI).

Formats (docs.github.com/en/copilot/reference/hooks-configuration, concepts/agents/cloud-agent/about-hooks):
  .github/hooks/*.json  {"version": 1, "hooks": {"preToolUse"|"agentStop": [{"type": "command",
                        "bash": "./x.sh", "command": "...", "exec": "...", "args": [...],
                        "cwd": ".", "timeoutSec": 30}]}}
The cloud agent honors `bash` (with `command` as fallback). A crashing preToolUse
command hook is denied (fail closed); a timeout fails open for every event.
There is no repo-level deny file (cloud agent firewall is a repo setting; CLI denies are flags).
"""
from __future__ import annotations

from typing import List, Tuple

from .registry import FAIL, Result, evaluator
from .agents import AgentSpec, register_agent
from .agent_common import (ACCEPTANCE, CI_ENFORCES, HookEntry, flat_list, judge_hooks,
                           register_common, register_na)

SPEC = register_agent(AgentSpec(
    id="copilot", name="GitHub Copilot",
    reads_agents_md="yes, natively (cloud agent and CLI)",
    edit_guard="preToolUse hook in `.github/hooks/*.json`",
    stop_gate="agentStop hook in `.github/hooks/*.json`",
    deny_rules="none at repo level (cloud firewall is a repo setting; CLI uses `--deny-tool` flags)",
    fail_mode="preToolUse fails closed on a crash, open on a timeout",
    user_once="trust the folder in Copilot CLI (`--trust-folder` for one headless run)",
    footprints=(".github/copilot-instructions.md", ".github/hooks/"),
    hook_file=".github/hooks/*.json", pretool_event="preToolUse", stop_event="agentStop",
    deny_file=None, verify_skill=".agents/skills/verify/SKILL.md",
    docs="docs.github.com/en/copilot/reference/hooks-configuration",
))
HOOK_DIR = ".github/hooks"

register_common(SPEC)


def _entries(ctx, event: str) -> Tuple[List[HookEntry], List[str]]:
    entries, errors = [], []
    for name in ctx.listdir(HOOK_DIR):
        if not name.endswith(".json"):
            continue
        rel = "%s/%s" % (HOOK_DIR, name)
        data, err = ctx.load_json(rel)
        if err or not isinstance(data, dict):
            errors.append("%s %s" % (rel, err or "top level is not an object"))
            continue
        for h in flat_list(data, event):
            if h.get("type", "command") != "command":
                continue
            if isinstance(h.get("bash"), str):
                cmd, args = h["bash"], None
            elif isinstance(h.get("command"), str):
                cmd, args = h["command"], None
            else:
                cmd, args = h.get("exec"), h.get("args")
            entries.append(HookEntry(rel, h, cmd, args, h.get("cwd")))
    return entries, errors


def _judge(ctx, event: str, note: str) -> Result:
    entries, errors = _entries(ctx, event)
    if not entries and errors:
        return Result(FAIL, "; ".join(errors[:2]), "Fix the JSON in %s" % HOOK_DIR)
    if not ctx.listdir(HOOK_DIR):
        return Result(FAIL, "no hook files in %s/" % HOOK_DIR,
                      "Run the advance skill for copilot; it writes %s/harness.json" % HOOK_DIR)
    return judge_hooks(ctx, SPEC, event, entries, note)


@evaluator("V2.stop_hook_effective", "copilot",
           how="a JSON file in .github/hooks/ has an `agentStop` command hook (`bash`, `command` or "
               "`exec`+`args`, resolved against `cwd`) whose script exists under .harness/hooks/.")
def _stop(ctx) -> Result:
    return _judge(ctx, SPEC.stop_event, "folder trust is required in Copilot CLI")


@evaluator("G3.pretool_guard_effective", "copilot",
           how="a JSON file in .github/hooks/ has a `preToolUse` command hook whose script exists "
               "under .harness/hooks/ (Copilot denies on a crash, allows on a timeout).")
def _pretool(ctx) -> Result:
    return _judge(ctx, SPEC.pretool_event,
                  "a crashing preToolUse hook denies (fail closed) but a timeout fails open; "
                  "folder trust is required in Copilot CLI")


NO_DENY = ("Copilot has no repo-level deny file (the cloud agent firewall is a repo setting and CLI "
           "denies are command-line flags); no in-session deny for this agent; %s" % CI_ENFORCES)


register_na("G3.deny_rules", "copilot", NO_DENY)
register_na("G3.acceptance_protected:deny", "copilot", NO_DENY,
            "%s (the preToolUse guard covers %s in session)" % (NO_DENY, ACCEPTANCE))
