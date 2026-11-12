"""Run checks, place the repo on the ladder, render JSON and the markdown scorecard."""
from __future__ import annotations

import os
from typing import List, Optional

from . import HF_VERSION, load_checks
from .registry import (NA, PASS, REQUIRED, RUNG_NAMES, UNKNOWN, Result,
                       get_check)

OK = (PASS, NA)


def run_checks(ctx, max_rung: int = 5) -> List[dict]:
    out = []
    for spec in load_checks():
        if spec.rung > max_rung:
            continue
        try:
            r = spec.fn(ctx)
            if not isinstance(r, Result):
                raise TypeError("check returned %r" % (r,))
        except Exception as e:  # a crashing check must never read as a pass
            r = Result(UNKNOWN, "check crashed: %s: %s" % (e.__class__.__name__, e),
                       "Report this as a harness-factory bug")
        fix = "" if r.status in OK else (r.fix if r.fix is not None else spec.fix)
        out.append({"id": spec.id, "rung": spec.rung, "title": spec.title,
                    "severity": spec.severity, "status": r.status, "evidence": r.evidence,
                    "fix": fix, "why": spec.why})
    return out


def place(checks: List[dict], max_rung: int = 5):
    """Return (rung_achieved, next_rung, blockers)."""
    achieved = 0
    for n in range(1, max_rung + 1):
        req = [c for c in checks if c["rung"] == n and c["severity"] == REQUIRED]
        if all(c["status"] in OK for c in req):
            achieved = n
        else:
            break
    nxt: Optional[int] = achieved + 1 if achieved < 5 else None
    blockers = [c["id"] for c in checks
                if nxt is not None and c["rung"] == nxt and c["severity"] == REQUIRED
                and c["status"] not in OK]
    return achieved, nxt, blockers


def build_report(ctx, max_rung: int = 5) -> dict:
    checks = run_checks(ctx, max_rung)
    achieved, nxt, blockers = place(checks, max_rung)
    d = ctx.detected
    return {
        "hf_version": HF_VERSION,
        "repo": ctx.root,
        "detected": {
            "ecosystems": d["ecosystems"],
            "package_managers": d["package_managers"],
            "commands": d["commands"],
            "command_sources": d["command_sources"],
            "workspaces": d["workspaces"],
            "agents": d["agents"],
            "configured_agents": ctx.agents,
            "default_branch": d["default_branch"],
        },
        "rung_achieved": achieved,
        "next_rung": nxt,
        "blockers": blockers,
        "checks": checks,
    }


# ---------------------------------------------------------------- markdown
def _cell(s: str, n: int = 100) -> str:
    s = (s or "").replace("|", "\\|").replace("\n", "; ")
    return s if len(s) <= n else s[: n - 3] + "..."


STATUS_LABEL = {"pass": "pass", "fail": "FAIL", "not_measured": "not measured",
                "unknown": "unknown", "na": "n/a"}


def render_markdown(rep: dict, ctx, max_rung: int = 5) -> str:
    L: List[str] = []
    a, nxt = rep["rung_achieved"], rep["next_rung"]
    name = os.path.basename(rep["repo"]) or rep["repo"]
    L.append("# Harness audit: %s" % name)
    L.append("")
    ach = "%d (%s)" % (a, RUNG_NAMES[a]) if a else "0 (none yet)"
    nx = "%d (%s)" % (nxt, RUNG_NAMES[nxt]) if nxt else "none, top of the ladder"
    L.append("**Rung achieved: %s**. Next rung: %s." % (ach, nx))
    d = rep["detected"]
    eco = ", ".join("%s (%s)" % (e, d["package_managers"][e]) if e in d["package_managers"] else e
                    for e in d["ecosystems"]) or "none"
    test = d["commands"].get("test") or "none found"
    L.append("Detected: %s; test `%s`; branch `%s`; agent config: %s; harness agents: %s."
             % (eco, test, d["default_branch"] or "?", ", ".join(d["agents"]) or "none",
                ", ".join(d["configured_agents"])))
    for agent in d["agents"]:
        if agent not in d["configured_agents"]:
            L.append("Hint: %s config found in the repo but `agents` in .harness/config.json does not list "
                     "%s; add it to audit that agent's hooks and rules." % (agent, agent))
    if d["workspaces"]:
        L.append("Monorepo: %s (v1 scores the repo root only)."
                 % ", ".join("%s %d members" % (w["tool"], len(w["members"])) for w in d["workspaces"]))
    mode = "ran gates/smoke/lint (--run)" if ctx.run else "static, no repo code executed (add --run to execute gates and smoke)"
    L.append("Mode: %s%s." % (mode, "; offline" if ctx.offline else ""))
    L.append("")

    by_id = {c["id"]: c for c in rep["checks"]}
    if nxt is not None:
        evaluated = any(c["rung"] == nxt for c in rep["checks"])
        L.append("## Blockers for rung %d (%s)" % (nxt, RUNG_NAMES[nxt]))
        L.append("")
        if not evaluated:
            L.append("Not evaluated (limited with --rung %d)." % max_rung)
        elif not rep["blockers"]:
            L.append("None.")
        for bid in rep["blockers"]:
            c = by_id[bid]
            L.append("- **%s** [%s] %s" % (bid, STATUS_LABEL[c["status"]], _cell(c["evidence"], 160)))
            L.append("  Why: %s" % c["why"])
            L.append("  Fix: %s" % c["fix"])
        L.append("")

    for n in range(1, max_rung + 1):
        rows = [c for c in rep["checks"] if c["rung"] == n]
        if not rows:
            continue
        passing = [c for c in rows if c["status"] == PASS]
        n_na = sum(1 for c in rows if c["status"] == NA)
        state = "achieved" if n <= a else ("next" if n == nxt else "later")
        # n/a is fine for achievement but is not shown as a pass
        L.append("## Rung %d %s: %s, %d/%d pass%s" % (n, RUNG_NAMES[n], state, len(passing), len(rows) - n_na,
                                                     ", %d n/a" % n_na if n_na else ""))
        if nxt is not None and n > nxt:
            open_ids = [c["id"].split(".", 1)[1] for c in rows if c["status"] not in OK]
            if open_ids:
                L.append("Open: %s" % ", ".join(open_ids))
            L.append("")
            continue
        rest = [c for c in rows if c["status"] not in OK and c["id"] not in rep["blockers"]]
        nb = sum(1 for c in rows if c["id"] in rep["blockers"])
        if nb:
            L.append("%d blocker(s) listed above." % nb)
        if rest:
            L.append("")
            L.append("| Check | Sev | Status | Evidence |")
            L.append("|---|---|---|---|")
            for c in rest:
                ev = _cell(c["evidence"], 90)
                L.append("| %s | %s | %s | %s |" % (c["id"], "req" if c["severity"] == REQUIRED else "rec",
                                                  STATUS_LABEL[c["status"]], ev))
        L.append("")
    L.append("Explain any check: `hf_audit.py --explain <ID>`. hf %s." % rep["hf_version"])
    return "\n".join(L) + "\n"


def explain(check_id: str) -> Optional[str]:
    load_checks()
    spec = get_check(check_id)
    if spec is None:
        return None
    return "\n".join([
        "%s (rung %d %s, %s)" % (spec.id, spec.rung, RUNG_NAMES[spec.rung], spec.severity),
        "",
        spec.title,
        "",
        "Why it matters: %s" % spec.why,
        "How it is detected: %s" % spec.how,
        "Fix: %s" % spec.fix,
    ] + _per_agent_how(spec)) + "\n"


def _per_agent_how(spec) -> List[str]:
    from .registry import evaluator_hows
    hows = evaluator_hows(spec.id)
    if not hows:
        return []
    out = ["", "Per agent (evaluated for each agent in config `agents`):"]
    for agent, how in hows.items():
        out.append("- %s: %s" % (agent, how or "as described above"))
    return out
