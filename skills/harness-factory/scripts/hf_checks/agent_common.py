"""Shared helpers for the non-Claude agent evaluators.

Hook commands are resolved statically: the agent's project-dir variable and a
`$(git rev-parse --show-toplevel)` wrapper become the repo root, `--agent ID`
arguments are dropped, and the target must be a file under .harness/hooks/.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Callable, Iterable, List, Optional, Tuple

from .registry import FAIL, NA, PASS, Result, evaluator
from .claude_cfg import INTERPRETERS
from .agents import AgentSpec
from . import installed

HOOKS_DIR = ".harness/hooks/"
ACCEPTANCE = ".harness/acceptance"
CI_ENFORCES = "the CI guard (diff-guard.sh in the guard workflow) and branch protection enforce"

_GIT_TOP = re.compile(r"\$\(\s*git\s+rev-parse\s+--show-toplevel\s*\)|`\s*git\s+rev-parse\s+--show-toplevel\s*`")


class HookEntry(object):
    """One registered command hook: where it is, what it runs."""

    def __init__(self, src: str, handler: dict, command: Optional[str],
                 args: Optional[list] = None, cwd: Optional[str] = None):
        self.src, self.handler, self.command, self.args, self.cwd = src, handler, command, args, cwd


def resolve_target(ctx, entry: HookEntry, root_vars: Iterable[str] = ()) -> Tuple[Optional[str], bool, str]:
    """Return (repo-relative target, needs_exec_bit, note)."""
    cmd = entry.command
    if not isinstance(cmd, str) or not cmd.strip():
        return None, False, "hook has no command"
    root = ctx.root.replace("\\", "/")
    s = _GIT_TOP.sub(root, cmd)
    for v in root_vars:
        s = re.sub(r"\$\{?%s\}?(?![A-Za-z0-9_])" % re.escape(v), root, s)
    try:
        tokens = shlex.split(s)
    except ValueError:
        return None, False, "cannot parse hook command"
    if isinstance(entry.args, list):
        tokens += [a for a in entry.args if isinstance(a, str)]
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    clean: List[str] = []
    skip = False
    for t in tokens:  # drop `--agent ID` and `--agent=ID`
        if skip:
            skip = False
            continue
        if t == "--agent":
            skip = True
            continue
        if t.startswith("--agent="):
            continue
        clean.append(t)
    if not clean:
        return None, False, "empty hook command"
    target, needs_exec = clean[0], True
    if os.path.basename(target) in INTERPRETERS or target in ("uv", "env"):
        rest = [t for t in clean[1:] if not t.startswith("-") and t != "run"]
        if not rest:
            return None, False, "interpreter with no script: %s" % cmd
        target, needs_exec = rest[0], False
    if "/" not in target:
        return None, False, "runs PATH command `%s`; cannot verify statically" % target
    if os.path.isabs(target):
        abs_t = target
    else:
        base = ctx.root
        if isinstance(entry.cwd, str) and entry.cwd.strip() and not os.path.isabs(entry.cwd):
            base = os.path.join(ctx.root, entry.cwd)
        abs_t = os.path.join(base, target)
    if not ctx.inside(abs_t):
        return None, False, "target %s is outside the repo; fresh clones will not have it" % target
    rel = os.path.relpath(os.path.realpath(abs_t), ctx.root).replace(os.sep, "/")
    return rel, needs_exec, ""


def hook_effective(ctx, entry: HookEntry, root_vars: Iterable[str] = ()) -> Tuple[bool, str]:
    rel, needs_exec, note = resolve_target(ctx, entry, root_vars)
    if rel is None:
        return False, note
    if not rel.startswith(HOOKS_DIR):
        return False, "hook target %s is not under %s" % (rel, HOOKS_DIR)
    if not ctx.isfile(rel):
        return False, "hook target %s does not exist" % rel
    changed = installed.ctx_guard_problem(ctx, rel)
    if changed:
        return False, "%s %s" % (rel, changed)
    if needs_exec:
        return ctx.executable(rel)
    return True, "%s exists%s" % (rel, ctx.uncommitted(rel))


def judge_hooks(ctx, spec: AgentSpec, event: str, entries: List[HookEntry], note: str,
                root_vars: Iterable[str] = (), fix: str = "",
                require: Optional[Callable[[HookEntry], Optional[str]]] = None) -> Result:
    """PASS when one entry is registered, meets `require`, and runs a real hook script."""
    fix = fix or "Run the advance skill for this agent; it writes %s and the scripts under %s" % (
        spec.hook_file, HOOKS_DIR)
    if not entries:
        return Result(FAIL, "no %s command hook registered in %s" % (event, spec.hook_file), fix)
    notes = []
    for e in entries:
        why_not = require(e) if require else None
        if why_not:
            notes.append(why_not)
            continue
        ok, why = hook_effective(ctx, e, root_vars)
        if ok:
            return Result(PASS, "%s hook `%s` in %s: %s; %s" % (event, e.command, e.src, why, note))
        notes.append(why)
    return Result(FAIL, "%s hook not effective: %s" % (event, "; ".join(notes[:3])), fix)


def nested_entries(data: dict, event: str, src: str) -> List[HookEntry]:
    """Claude-style shape {"hooks": {Event: [{"matcher", "hooks": [{"type": "command", "command"}]}]}}."""
    out = []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    for g in groups if isinstance(groups, list) else []:
        for h in g.get("hooks", []) if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []:
            if isinstance(h, dict) and h.get("type", "command") == "command":
                out.append(HookEntry(src, h, h.get("command")))
    return out


def flat_list(data: dict, event: str) -> List[dict]:
    """Flat shape {"version": 1, "hooks": {event: [{...handler...}]}} (Cursor, Copilot)."""
    hooks = data.get("hooks") if isinstance(data, dict) else None
    items = hooks.get(event) if isinstance(hooks, dict) else None
    return [h for h in items if isinstance(h, dict)] if isinstance(items, list) else []


def na(text: str) -> Result:
    return Result(NA, text)


def register_na(check_id: str, agent: str, msg: str, evidence: Optional[str] = None) -> None:
    """Register an evaluator that always answers n/a.

    --explain shows "n/a: <msg>."; the result evidence is `evidence`, or msg itself.
    """
    text = msg if evidence is None else evidence

    @evaluator(check_id, agent, how="n/a: " + msg + ".")
    def _na(ctx) -> Result:
        return na(text)


def register_common(spec: AgentSpec, wired: Optional[Callable] = None) -> None:
    """Register the evaluators that are the same shape for every non-Claude agent."""

    @evaluator("V2.verify_skill", spec.id,
               how="%s exists and is not empty (the Agent Skills folder this agent reads)." % spec.verify_skill)
    def _verify(ctx) -> Result:
        rel = spec.verify_skill
        t = ctx.read_text(rel)
        if t is not None and t.strip():
            return Result(PASS, rel + ctx.uncommitted(rel))
        return Result(FAIL, "%s missing or empty" % rel,
                      "Run the advance skill for rung 2 to install %s" % rel)

    if wired is None:
        @evaluator("L1.agent_doc_wired", spec.id,
                   how="%s reads AGENTS.md natively; passes when AGENTS.md exists." % spec.name)
        def _wired(ctx) -> Result:
            if not ctx.isfile("AGENTS.md"):
                return Result(NA, "no AGENTS.md")
            return Result(PASS, "%s reads AGENTS.md natively" % spec.name)

    @evaluator("G3.sandbox_settings", spec.id,
               how="Not audited for %s; the sandbox check covers Claude Code settings only." % spec.name)
    def _sandbox(ctx) -> Result:
        return Result(NA, "sandbox settings are not audited for %s yet; check its sandbox mode by hand"
                      % spec.name)
