"""Claude Code adapter helpers: hook registration and permission rules.

Shapes follow the Claude Code hooks and permissions docs:
  {"hooks": {"<Event>": [{"matcher": "...", "hooks": [{"type": "command",
   "command": "...", "args": [...], "onFailure": "block"}]}]}}
  {"permissions": {"deny": ["Bash(gh pr merge *)", "Edit(.env)"]}}
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Iterator, List, Optional, Tuple

from . import installed

INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3", "node", "ruby",
                "perl", "bun", "deno", "pwsh"}


def hook_handlers(settings: dict, event: str) -> Iterator[dict]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return
    for g in groups:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            for h in g["hooks"]:
                if isinstance(h, dict):
                    yield h


def resolve_hook_target(ctx, handler: dict) -> Tuple[Optional[str], bool, str]:
    """Map a command hook to the repo file it runs.

    Returns (repo-relative path or None, needs_exec_bit, note).
    """
    cmd = handler.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None, False, "hook has no command"
    if "CLAUDE_PLUGIN_ROOT" in cmd:
        return None, False, "runs a plugin script; cloud runs do not load plugins from the repo"
    expanded = re.sub(r"\$\{?CLAUDE_PROJECT_DIR\}?", ctx.root.replace("\\", "/"), cmd)
    try:
        tokens = shlex.split(expanded)
    except ValueError:
        return None, False, "cannot parse hook command"
    args = handler.get("args")
    if isinstance(args, list):
        tokens += [a for a in args if isinstance(a, str)]
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)  # leading VAR=value assignments
    if not tokens:
        return None, False, "empty hook command"
    target, needs_exec = tokens[0], True
    if os.path.basename(target) in INTERPRETERS or target in ("uv", "env"):
        rest = [t for t in tokens[1:] if not t.startswith("-") and t != "run"]
        if not rest:
            return None, False, "interpreter with no script: %s" % cmd
        target, needs_exec = rest[0], False
    if "/" not in target:
        return None, False, "runs PATH command `%s`; cannot verify statically" % target
    abs_t = target if os.path.isabs(target) else os.path.join(ctx.root, target)
    if not ctx.inside(abs_t):
        return None, False, "target %s is outside the repo; fresh clones will not have it" % target
    rel = os.path.relpath(os.path.realpath(abs_t), ctx.root).replace(os.sep, "/")
    return rel, needs_exec, ""


def hook_effective(ctx, handler: dict) -> Tuple[bool, str]:
    rel, needs_exec, note = resolve_hook_target(ctx, handler)
    if rel is None:
        return False, note
    if not ctx.isfile(rel):
        return False, "hook target %s does not exist" % rel
    changed = installed.ctx_guard_problem(ctx, rel)
    if changed:
        return False, "%s %s" % (rel, changed)
    if needs_exec:
        ok, why = ctx.executable(rel)
        return ok, why
    return True, "%s exists%s" % (rel, ctx.uncommitted(rel))


# ---------------------------------------------------------------- permissions
_RULE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?\s*$", re.S)


def parse_rule(rule: str) -> Tuple[str, Optional[str]]:
    m = _RULE.match(rule)
    if not m:
        return rule, None
    return m.group(1), m.group(2)


def bash_rule_matches(spec: Optional[str], command: str) -> bool:
    """Does a Bash(...) specifier match a command line?

    `*` matches any run of characters (including spaces); the legacy `:*`
    suffix is a prefix match. A bare `Bash` rule (spec None) matches all.
    """
    if spec is None or spec.strip() in ("", "*"):
        return True
    spec = spec.strip()
    if spec.endswith(":*"):
        prefix = spec[:-2]
        return command == prefix or command.startswith(prefix + " ")
    pattern = "^" + ".*".join(re.escape(p) for p in spec.split("*")) + "$"
    return re.match(pattern, command, re.S) is not None


def deny_rules(settings: dict, kind: str = "deny") -> List[str]:
    """Rules of one kind ("deny" or "ask") from permissions."""
    perms = settings.get("permissions")
    if not isinstance(perms, dict) or not isinstance(perms.get(kind), list):
        return []
    return [r for r in perms[kind] if isinstance(r, str)]


def denies_command(rules: List[str], command: str) -> Optional[str]:
    for r in rules:
        tool, spec = parse_rule(r)
        if tool == "Bash" and bash_rule_matches(spec, command):
            return r
    return None


def denies_path_edit(rules: List[str], path_fragment: str) -> List[str]:
    """Deny rules on edit tools whose path pattern names the fragment."""
    hits = []
    frag = path_fragment.strip("/")
    for r in rules:
        tool, spec = parse_rule(r)
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            if spec is None or frag in spec:
                hits.append(r)
    return hits
