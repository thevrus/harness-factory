"""Rung 4, First loop: one agent workflow that can be switched off, is capped and attributed."""
from __future__ import annotations

import re

from .registry import FAIL, NA, PASS, UNKNOWN, Result, check
from .rung2 import pr_templates
from .workflows import agent_workflows, is_agent_text, job_if, split_jobs

KILL = "vars.AGENT_ENABLED"


@check("F4.agent_workflow", 4,
       "At least one CI workflow runs the agent",
       fix="Run the advance skill for rung 4 to install the first agent workflow in .github/workflows/",
       why="This rung is about letting the agent work from CI; without a workflow there is no loop to run.",
       how="Looks in .github/workflows/ for a workflow that uses anthropics/claude-code-action or calls the claude CLI (comment lines ignored).")
def agent_workflow(ctx) -> Result:
    wfs = agent_workflows(ctx)
    if wfs:
        return Result(PASS, ", ".join(w.path for w in wfs))
    return Result(FAIL, "no agent workflow in .github/workflows/")


@check("F4.kill_switch", 4,
       "Every agent workflow can be switched off with one setting (AGENT_ENABLED)",
       fix="Add `if: vars.AGENT_ENABLED == 'true'` to each agent job and create the repo variable AGENT_ENABLED under Settings > Secrets and variables > Actions",
       why="When the agent misbehaves you need to stop it in one click, not by editing every workflow.",
       how="For each job in an agent workflow that invokes the agent, checks the job-level `if:` mentions vars.AGENT_ENABLED (comments ignored; a mention elsewhere, such as an env value or a step, does not count).")
def kill_switch(ctx) -> Result:
    wfs = agent_workflows(ctx)
    if not wfs:
        return Result(NA, "no agent workflows")
    bad, n = [], 0
    for w in wfs:
        jobs = split_jobs(w.text)
        if not jobs:
            bad.append("%s: no jobs: block found" % w.path)
            continue
        for name, body in jobs.items():
            if not is_agent_text(body):
                continue
            n += 1
            if KILL not in job_if(body):
                bad.append("%s job %s" % (w.path, name))
    if bad:
        return Result(FAIL, "no %s in the job-level if: of %s" % (KILL, "; ".join(bad[:4])))
    if n == 0:
        return Result(UNKNOWN, "agent workflows found but no job that invokes the agent could be located")
    return Result(PASS, "%d agent job(s) gate on %s in their if:" % (n, KILL))


@check("F4.maintainers", 4,
       "A list of people allowed to start the agent (.harness/maintainers.txt)",
       fix="Create .harness/maintainers.txt with one GitHub username per line",
       why="Without it, anyone who can open an issue might be able to set the agent to work.",
       how="Checks .harness/maintainers.txt has at least one line that is not blank or a comment.")
def maintainers(ctx) -> Result:
    rel = ".harness/maintainers.txt"
    lines = ctx.meaningful_lines(rel)
    if lines:
        return Result(PASS, "%d maintainer(s)%s" % (len(lines), ctx.uncommitted(rel)))
    return Result(FAIL, "%s missing or empty" % rel)


@check("F4.attribution", 4,
       "Agent pull requests are labelled agent:* and carry an Agent-Run: line",
       fix="Make the agent workflow add an `agent:<name>` label and an `Agent-Run: <run id>` line to each PR it opens (the rung 4 template does this)",
       why="Without a label you cannot tell agent work from human work, so you cannot measure whether the agent helps.",
       how="Looks in the PR templates, agent workflows and .harness/bin/agent-output.py (which writes the Agent-Run: line on every comment the shipped loops post) for an agent: label pattern and the text Agent-Run:.")
def attribution(ctx) -> Result:
    texts = [(t, ctx.read_text(t) or "") for t in pr_templates(ctx)]
    wfs = agent_workflows(ctx)
    texts += [(w.path, w.text) for w in wfs]
    # Only counts when a workflow calls it, so a stray copy cannot pass the check.
    out = ".harness/bin/agent-output.py"
    if any("agent-output.py" in w.text for w in wfs) and ctx.isfile(out):
        texts.append((out, ctx.read_text(out) or ""))
    label = next((p for p, t in texts if re.search(r"\bagent:[\w*<{$-]", t)), None)
    trailer = next((p for p, t in texts if "Agent-Run:" in t), None)
    if label and trailer:
        return Result(PASS, "agent: label in %s; Agent-Run: in %s" % (label, trailer))
    miss = [n for n, v in (("agent:* label", label), ("Agent-Run: trailer", trailer)) if not v]
    return Result(FAIL, "missing %s" % " and ".join(miss))


@check("F4.budget_caps", 4,
       "Every agent job has a time limit and a turn limit",
       fix="Add `timeout-minutes: 30` to each agent job and `--max-turns 30` to its claude_args",
       why="Without limits a stuck agent can run for hours and spend a lot of money.",
       how="For each job in an agent workflow that invokes the agent, looks for timeout-minutes: and --max-turns in that job.")
def budget_caps(ctx) -> Result:
    wfs = agent_workflows(ctx)
    if not wfs:
        return Result(NA, "no agent workflows")
    bad, n = [], 0
    for w in wfs:
        jobs = split_jobs(w.text) or {"(file)": w.text}
        for name, body in jobs.items():
            if not is_agent_text(body):
                continue
            n += 1
            miss = [k for k in ("timeout-minutes:", "--max-turns") if k not in body]
            if miss:
                bad.append("%s job %s lacks %s" % (w.path, name, " and ".join(miss)))
    if bad:
        return Result(FAIL, "; ".join(bad[:4]))
    if n == 0:
        return Result(UNKNOWN, "agent workflows found but no job that invokes the agent could be located")
    return Result(PASS, "%d agent job(s) have timeout-minutes and --max-turns" % n)


@check("F4.ledger", 4,
       "A log of audits and installs exists (.harness/ledger.jsonl)",
       fix="Run `python3 scripts/hf_audit.py --repo . --append-ledger` from the plugin, or the advance skill, to start .harness/ledger.jsonl",
       why="Without a log you cannot see how the harness changed over time or what it was when something broke.",
       how="Checks .harness/ledger.jsonl exists.")
def ledger(ctx) -> Result:
    rel = ".harness/ledger.jsonl"
    if ctx.isfile(rel):
        return Result(PASS, "%s (%d entries)%s" % (rel, len(ctx.meaningful_lines(rel)), ctx.uncommitted(rel)))
    return Result(FAIL, "%s missing" % rel)
