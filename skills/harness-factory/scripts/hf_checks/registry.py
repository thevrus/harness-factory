"""Check registry and result type.

Each check is a small function taking a RepoContext and returning a Result.
Registration order is report order.
"""
from __future__ import annotations

from typing import Callable, Dict, List, NamedTuple, Optional

PASS = "pass"
FAIL = "fail"
NOT_MEASURED = "not_measured"
UNKNOWN = "unknown"
NA = "na"

REQUIRED = "required"
RECOMMENDED = "recommended"

RUNG_NAMES = {1: "Legible", 2: "Verifiable", 3: "Guarded", 4: "First loop", 5: "Factory"}
RUNG_PREFIX = {1: "L1", 2: "V2", 3: "G3", 4: "F4", 5: "X5"}


class Result(NamedTuple):
    status: str
    evidence: str
    fix: Optional[str] = None  # None means "use the registered default fix"


class CheckSpec(NamedTuple):
    id: str
    rung: int
    severity: str
    title: str
    fix: str
    why: str   # one plain sentence: what goes wrong without this
    how: str   # how the audit detects it
    fn: Callable


_REGISTRY: List[CheckSpec] = []
_IDS: Dict[str, CheckSpec] = {}


def check(id: str, rung: int, title: str, fix: str, why: str, how: str,
          severity: str = REQUIRED):
    """Register a check. `fix` is the default one-line remedy."""
    if not id.startswith(RUNG_PREFIX[rung] + "."):
        raise ValueError("check id %s does not match rung %d" % (id, rung))
    if severity not in (REQUIRED, RECOMMENDED):
        raise ValueError("bad severity for %s" % id)

    def deco(fn):
        if id in _IDS:
            raise ValueError("duplicate check id %s" % id)
        if not why or not how:
            raise ValueError("check %s needs why and how" % id)
        spec = CheckSpec(id, rung, severity, title, fix, why, how, fn)
        _REGISTRY.append(spec)
        _IDS[id] = spec
        return fn

    return deco


def all_checks() -> List[CheckSpec]:
    return list(_REGISTRY)


def get_check(id: str) -> Optional[CheckSpec]:
    return _IDS.get(id)


# ------------------------------------------------------------ per-agent checks
# Agent-specific checks (hooks, permissions, doc wiring) are evaluated once per
# agent listed in config `agents`. Each (check, agent) pair is one function, so
# supporting a new agent means adding evaluators, not editing the checks.

_EVALUATORS: Dict[str, Dict[str, Callable]] = {}
_EVAL_HOW: Dict[str, Dict[str, str]] = {}

# Worst status wins when combining agents.
_SEVERITY_ORDER = {FAIL: 4, UNKNOWN: 3, NOT_MEASURED: 2, PASS: 1, NA: 0}


def evaluator(check_id: str, agent: str, how: str = ""):
    """Register the evaluator of one (check, agent) pair. `how` feeds --explain."""
    def deco(fn):
        _EVALUATORS.setdefault(check_id, {})[agent] = fn
        if how:
            _EVAL_HOW.setdefault(check_id, {})[agent] = how
        return fn
    return deco


def evaluator_hows(check_id: str) -> Dict[str, Optional[str]]:
    """Agents with an evaluator for this check (and its `:deny` part), with their `how` text."""
    out: Dict[str, Optional[str]] = {}
    for cid in (check_id, check_id + ":deny"):
        for agent in _EVALUATORS.get(cid, {}):
            how = _EVAL_HOW.get(cid, {}).get(agent)
            prev = out.get(agent)
            out[agent] = "; ".join(x for x in (prev, how) if x) or None
    return out


def agent_check(id: str, rung: int, title: str, fix: str, why: str, how: str,
                severity: str = REQUIRED) -> None:
    """Register a check whose result is the combination of its per-agent evaluators."""

    def run(ctx) -> Result:
        return evaluate_per_agent(ctx, id)

    check(id, rung, title, fix, why, how + " Evaluated once per agent in config `agents`.",
          severity)(run)


def evaluate_per_agent(ctx, check_id: str) -> Result:
    """Combine per-agent results: the worst status wins, so any fail or unknown in a
    configured agent fails or blocks the check, and one agent's n/a never hides
    another agent's fail. With several agents, evidence has one line per agent."""
    agents = ctx.agents or ["claude"]
    table = _EVALUATORS.get(check_id, {})
    spec = _IDS.get(check_id.split(":", 1)[0])
    results = []
    for agent in agents:
        fn = table.get(agent)
        if fn is None:
            r = Result(UNKNOWN, "no evaluator for agent %s yet" % agent,
                       "audit support for agent %s is not implemented yet; check it by hand" % agent)
        else:
            r = fn(ctx)
        results.append((agent, r))
    worst = max((r for _, r in results), key=lambda r: _SEVERITY_ORDER[r.status])
    if len(results) == 1:
        return worst
    evidence = "\n".join("%s: %s" % (a, r.evidence) for a, r in results)
    fixes = []
    for a, r in results:
        if r.status in (PASS, NA):
            continue
        f = r.fix if r.fix is not None else (spec.fix if spec else None)
        if f:
            fixes.append("%s: %s" % (a, f))
    return Result(worst.status, evidence, "; ".join(fixes) if fixes else worst.fix)
