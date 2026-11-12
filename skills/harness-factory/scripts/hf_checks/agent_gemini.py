"""Gemini CLI evaluators.

Formats (github.com/google-gemini/gemini-cli docs/cli/gemini-md.md, docs/hooks/reference.md,
docs/reference/policy-engine.md):
  .gemini/settings.json  {"context": {"fileName": ["AGENTS.md", "GEMINI.md"]},
                          "hooks": {"BeforeTool"|"AfterAgent": [{"matcher": "...",
                          "hooks": [{"type": "command", "command": "$GEMINI_PROJECT_DIR/..."}]}]}}
  GEMINI.md may instead import AGENTS.md with `@./AGENTS.md`.
Hook exits other than 0 and 2 fail open. Workspace policies (.gemini/policies) are
documented as non-functional, so there is no reliable repo-level deny file.
"""
from __future__ import annotations

from typing import Optional, Tuple

from .registry import FAIL, NA, PASS, Result, evaluator
from .agents import AgentSpec, register_agent
from .agent_common import (ACCEPTANCE, CI_ENFORCES, judge_hooks, nested_entries, register_common,
                           register_na)

SPEC = register_agent(AgentSpec(
    id="gemini", name="Gemini CLI",
    reads_agents_md="only if `.gemini/settings.json` `context.fileName` lists it (or GEMINI.md imports `@./AGENTS.md`)",
    edit_guard="BeforeTool hook in `.gemini/settings.json`",
    stop_gate="AfterAgent hook in `.gemini/settings.json`",
    deny_rules="none reliable (workspace policies are documented as non-functional)",
    fail_mode="fails open (exits other than 0 and 2 are warnings)",
    user_once="trust the folder if Trusted Folders is on; accept changed project hooks",
    footprints=(".gemini/", "GEMINI.md"),
    hook_file=".gemini/settings.json", pretool_event="BeforeTool", stop_event="AfterAgent",
    deny_file=None, verify_skill=".agents/skills/verify/SKILL.md",
    docs="gemini-cli docs/cli/gemini-md.md, docs/hooks/reference.md, docs/reference/policy-engine.md",
))
ROOT_VARS = ("GEMINI_PROJECT_DIR", "CLAUDE_PROJECT_DIR")
NOTE = ("Gemini hooks fail open on a crash; with Trusted Folders on, an untrusted folder ignores "
        "workspace settings")


def _settings(ctx) -> Tuple[Optional[dict], Optional[str]]:
    data, err = ctx.load_json(SPEC.hook_file)
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "top level is not an object"
    return data, None


def _wired(ctx) -> Result:
    if not ctx.isfile("AGENTS.md"):
        return Result(NA, "no AGENTS.md")
    data, err = _settings(ctx)
    if data is not None:
        ctxcfg = data.get("context") if isinstance(data.get("context"), dict) else {}
        names = ctxcfg.get("fileName")
        names = [names] if isinstance(names, str) else names if isinstance(names, list) else []
        if "AGENTS.md" in names:
            return Result(PASS, "%s context.fileName includes AGENTS.md" % SPEC.hook_file)
    if ctx.isfile("GEMINI.md"):
        from .rung1 import imports_of
        for line, target in imports_of(ctx, "GEMINI.md"):
            if target == "AGENTS.md":
                return Result(PASS, "GEMINI.md:%d imports AGENTS.md" % line)
    return Result(FAIL, "Gemini CLI does not read AGENTS.md: %s context.fileName does not list it "
                  "and no GEMINI.md imports it" % SPEC.hook_file,
                  "Set {\"context\": {\"fileName\": [\"AGENTS.md\", \"GEMINI.md\"]}} in %s" % SPEC.hook_file)


evaluator("L1.agent_doc_wired", "gemini",
          how="%s context.fileName (string or list) includes AGENTS.md, or GEMINI.md imports "
              "@./AGENTS.md outside code blocks." % SPEC.hook_file)(_wired)
register_common(SPEC, wired=_wired)


def _judge(ctx, event: str) -> Result:
    data, err = _settings(ctx)
    if err:
        return Result(FAIL, "%s %s" % (SPEC.hook_file, err),
                      "Run the advance skill for gemini; it writes the hooks into %s" % SPEC.hook_file)
    return judge_hooks(ctx, SPEC, event, nested_entries(data, event, SPEC.hook_file), NOTE,
                       root_vars=ROOT_VARS)


@evaluator("V2.stop_hook_effective", "gemini",
           how="%s hooks.AfterAgent has a command hook whose script (after stripping "
               "$GEMINI_PROJECT_DIR and --agent ID) exists under .harness/hooks/." % SPEC.hook_file)
def _stop(ctx) -> Result:
    return _judge(ctx, SPEC.stop_event)


@evaluator("G3.pretool_guard_effective", "gemini",
           how="%s hooks.BeforeTool has a command hook whose script exists under .harness/hooks/; "
               "passes with a note that Gemini hooks fail open." % SPEC.hook_file)
def _pretool(ctx) -> Result:
    return _judge(ctx, SPEC.pretool_event)


NO_DENY = ("Gemini CLI has no reliable repo-level deny file (workspace policies are documented as "
           "non-functional); no in-session deny for this agent; %s" % CI_ENFORCES)


register_na("G3.deny_rules", "gemini", NO_DENY)
register_na("G3.acceptance_protected:deny", "gemini", NO_DENY,
            "%s (BeforeTool guard covers %s in session, failing open)" % (NO_DENY, ACCEPTANCE))
