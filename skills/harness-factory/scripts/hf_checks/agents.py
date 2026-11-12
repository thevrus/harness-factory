"""Per-agent capability data: one record per coding agent the audit knows.

Each agent module (agent_<id>.py, plus the Claude record below) registers an
AgentSpec. The evaluators read their file paths and event names from it, and
`hf_audit.py --agents` renders the capability table from it, so the docs and
the checks cannot drift apart.
"""
from __future__ import annotations

import importlib
import os
from typing import Dict, List, NamedTuple, Optional, Tuple


class AgentSpec(NamedTuple):
    id: str
    name: str
    # Capability table columns (plain text, rendered as is).
    reads_agents_md: str
    edit_guard: str
    stop_gate: str
    deny_rules: str
    fail_mode: str
    user_once: str
    # Files and events the evaluators use. None means the agent has no such capability.
    footprints: Tuple[str, ...]   # repo paths that show the agent is configured
    hook_file: Optional[str]
    pretool_event: Optional[str]
    stop_event: Optional[str]
    deny_file: Optional[str]
    verify_skill: str
    docs: str                     # where the formats come from


_AGENTS: Dict[str, AgentSpec] = {}
_LOADED = False


def register_agent(spec: AgentSpec) -> AgentSpec:
    _AGENTS[spec.id] = spec
    return spec


def all_agents() -> List[AgentSpec]:
    load_agents()
    return list(_AGENTS.values())


def load_agents() -> None:
    """Import every agent_<id>.py next to this file (importing registers it)."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    here = os.path.dirname(os.path.abspath(__file__))
    for name in sorted(os.listdir(here)):
        if name.startswith("agent_") and name.endswith(".py"):
            importlib.import_module("%s.%s" % (__package__, name[:-3]))


CLAUDE = register_agent(AgentSpec(
    id="claude", name="Claude Code",
    reads_agents_md="only through `@AGENTS.md` in CLAUDE.md",
    edit_guard="PreToolUse hook in `.claude/settings.json`",
    stop_gate="Stop hook in `.claude/settings.json`",
    deny_rules="`.claude/settings.json` `permissions.deny` / `ask`",
    fail_mode="blocks when the hook sets `\"onFailure\": \"block\"`",
    user_once="nothing",
    footprints=(".claude/", "CLAUDE.md"),
    hook_file=".claude/settings.json", pretool_event="PreToolUse", stop_event="Stop",
    deny_file=".claude/settings.json", verify_skill=".claude/skills/verify/SKILL.md",
    docs="code.claude.com/docs (hooks, permissions)",
))


def capability_table() -> str:
    rows = ["| Agent id | Reads AGENTS.md | In-session edit guard | In-session stop gate "
            "| Repo-level deny rules | Fail mode on hook crash | User must do once |",
            "|---|---|---|---|---|---|---|"]
    for a in all_agents():
        cells = [a.id, a.reads_agents_md, a.edit_guard, a.stop_gate, a.deny_rules,
                 a.fail_mode, a.user_once]
        rows.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(rows) + "\n"
