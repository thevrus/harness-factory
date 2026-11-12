"""Rung 5, Factory: agents implement, are monitored, and the harness learns from failures.

Paths match what templates/rung5 installs: .harness/bin/metrics.py,
.harness/rules-inbox.md, .harness/failures/<case>/ directories (README.md is
not a case) and agent-implement.yml / agent-monitor.yml. Close variants are
accepted.
"""
from __future__ import annotations

import re

from .registry import FAIL, PASS, RECOMMENDED, UNKNOWN, Result, check
from .context import has_marker
from .workflows import agent_workflows, load_workflows


@check("X5.implement_lane", 5,
       "An agent workflow that writes code, started only by a listed maintainer",
       fix="Run the advance skill for rung 5 to install the implement workflow; it starts on a label or manual run and checks .harness/maintainers.txt",
       why="Letting anyone trigger code-writing agents lets strangers spend your budget and steer your code.",
       how="Looks for an agent workflow triggered by `labeled` or `workflow_dispatch` that references .harness/maintainers.txt.")
def implement_lane(ctx) -> Result:
    cands = []
    for w in agent_workflows(ctx):
        trig = re.search(r"\blabeled\b|workflow_dispatch", w.text)
        if trig and "maintainers.txt" in w.text:
            return Result(PASS, "%s (trigger %s, maintainer check)" % (w.path, trig.group(0)))
        if trig:
            cands.append(w.path)
    if cands:
        return Result(FAIL, "%s start on a label/dispatch but do not check .harness/maintainers.txt"
                      % ", ".join(cands))
    return Result(FAIL, "no agent workflow triggered by a label or manual dispatch")


@check("X5.monitor", 5,
       "A scheduled job watches the agents' results",
       fix="Run the advance skill for rung 5 to install the scheduled monitor workflow in .github/workflows/",
       why="Without someone or something looking regularly, a broken agent keeps opening bad pull requests unnoticed.",
       how="Looks for a workflow with a schedule (cron) that runs the agent or a .harness/bin/ script.")
def monitor(ctx) -> Result:
    for w in load_workflows(ctx):
        if re.search(r"(?m)^\s*schedule:", w.text) and "cron" in w.text:
            if ".harness/bin/" in w.text or w in agent_workflows(ctx):
                return Result(PASS, "%s runs on a schedule" % w.path)
    return Result(FAIL, "no scheduled workflow runs the agent or a harness script")


@check("X5.metrics_script", 5,
       "A script that measures how well the agents do (.harness/bin/metrics.*)",
       fix="Run the advance skill for rung 5 to install .harness/bin/metrics.py",
       why="Without numbers you cannot tell whether the agents save time or create more work.",
       how="Looks for .harness/bin/metrics.py, metrics.sh or another .harness/bin/metrics* file without the HARNESS_STUB marker.")
def metrics_script(ctx) -> Result:
    hits = [".harness/bin/" + n for n in ctx.listdir(".harness/bin") if n.startswith("metrics")]
    if not hits:
        return Result(FAIL, "no .harness/bin/metrics* script")
    real = [h for h in hits if not has_marker(ctx, h)]
    if not real:
        return Result(FAIL, "%s is a stub (HARNESS_STUB)" % hits[0])
    return Result(PASS, real[0] + ctx.uncommitted(real[0]))


@check("X5.failure_library", 5,
       "Past agent failures are written down as cases (.harness/failures/)",
       fix="Add one file per agent failure to .harness/failures/ (what happened, how it was caught, the rule that prevents it); aim for 20",
       why="If failures are not recorded, the same mistakes come back and nobody notices the pattern.",
       how="Counts files in .harness/failures/ other than README and .gitkeep; at least 1 passes, 20 or more is the goal.")
def failure_library(ctx) -> Result:
    d = ".harness/failures"
    cases = [n for n in ctx.listdir(d)
             if not n.startswith(".") and not n.lower().startswith(("readme", "index", "template"))]
    if not cases:
        return Result(FAIL, "%s has no cases" % d)
    note = "" if len(cases) >= 20 else " (goal: 20 or more)"
    return Result(PASS, "%d case(s) in %s%s" % (len(cases), d, note))


@check("X5.rules_inbox", 5,
       "A place where repeated mistakes become proposed rules (.harness/rules-inbox.md)",
       fix="Create .harness/rules-inbox.md (the rung 5 template does) and let the monitor add rule candidates there",
       why="Without it, lessons from failures stay in people's heads instead of becoming checks.",
       how="Checks .harness/rules-inbox.md or a .harness/rules-inbox/ directory exists.")
def rules_inbox(ctx) -> Result:
    for rel in (".harness/rules-inbox.md", ".harness/rules-inbox"):
        if ctx.exists(rel):
            return Result(PASS, rel + ctx.uncommitted(rel))
    return Result(FAIL, ".harness/rules-inbox.md missing")


@check("X5.holdout", 5,
       "Some acceptance scenarios are hidden from the agent (holdout)",
       fix="Keep holdout tasks outside the agent-readable tree (a separate private repository or an encrypted CI artifact) and read them only from an eval job, as .harness/docs/factory.md describes",
       why="An agent that can read every test can learn to pass the tests instead of solving the problem.",
       how="Per .harness/docs/factory.md the holdout lives outside the repo, so a static audit cannot see it. Fails when a holdout directory sits in the repo (agent-readable), passes when a workflow references a holdout (the eval job), otherwise unknown.",
       severity=RECOMMENDED)
def holdout(ctx) -> Result:
    inside = [d for d in ctx.files if re.search(r"(?:^|/)holdouts?/", d, re.I)]
    if inside:
        return Result(FAIL, "holdout files inside the agent-readable tree: %s" % inside[0],
                      "Move them to a private repository or encrypted CI artifact (see .harness/docs/factory.md)")
    for w in load_workflows(ctx):
        if re.search(r"holdout", w.text, re.I):
            return Result(PASS, "%s references a holdout (eval job)" % w.path)
    return Result(UNKNOWN, "holdout lives outside the repo by convention; no eval workflow references one")
