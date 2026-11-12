"""OpenCode evaluators.

OpenCode reads AGENTS.md natively (opencode.ai/docs/instructions.md). It has no
shell-command hooks: hooks are TypeScript plugins (opencode.ai/docs/build/plugins/server.md),
which this audit does not evaluate, and no turn-end stop hook is documented. Repo
permission rules exist in opencode.jsonc but are not audited in this version.
"""
from __future__ import annotations

from .agents import AgentSpec, register_agent
from .agent_common import ACCEPTANCE, CI_ENFORCES, register_common, register_na

SPEC = register_agent(AgentSpec(
    id="opencode", name="OpenCode",
    reads_agents_md="yes, natively (V2 reads AGENTS.md only)",
    edit_guard="none (no command hooks; TypeScript plugins only)",
    stop_gate="none documented",
    deny_rules="not audited in this version",
    fail_mode="n/a (no command hooks)",
    user_once="nothing",
    footprints=("opencode.json", "opencode.jsonc", ".opencode/"),
    hook_file=None, pretool_event=None, stop_event=None,
    deny_file=None, verify_skill=".agents/skills/verify/SKILL.md",
    docs="opencode.ai/docs/instructions.md, opencode.ai/docs/build/plugins/server.md",
))

register_common(SPEC)

NO_GUARD = "OpenCode has no command hooks; no in-session guard for this agent; %s" % CI_ENFORCES
NO_STOP = ("OpenCode has no command hooks and no documented stop hook; no in-session stop gate for "
           "this agent; the CI gates and the PR evidence check enforce")
NO_DENY = "OpenCode deny rules are not audited in this version; no in-session deny is checked; %s" % CI_ENFORCES


register_na("V2.stop_hook_effective", "opencode", NO_STOP)
register_na("G3.pretool_guard_effective", "opencode", NO_GUARD)
register_na("G3.deny_rules", "opencode", NO_DENY)
register_na("G3.acceptance_protected:deny", "opencode", NO_DENY,
            "%s (covers %s)" % (NO_DENY, ACCEPTANCE))
