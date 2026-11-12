#!/usr/bin/env python3
"""Audit a repo against the harness-factory ladder.

    hf_audit.py --repo PATH [--json OUT] [--run] [--rung N] [--append-ledger]
                [--offline] [--timeout SECONDS]
    hf_audit.py --explain CHECK_ID
    hf_audit.py --agents

Default is static and read-only: it never executes repo code and never writes
into the repo. --run executes only .harness/bin/gates.sh, .harness/bin/smoke.sh
and .harness/bin/lint-agent-workflows.py. --append-ledger is the only write.
Exit 0 always, unless the tool itself errors (exit 2).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

# Keep the plugin directory clean: no __pycache__ next to the scripts.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hf_checks import HF_VERSION  # noqa: E402
from hf_checks.context import RepoContext  # noqa: E402
from hf_checks.registry import NA, PASS  # noqa: E402
from hf_checks.report import build_report, explain, render_markdown  # noqa: E402


def parse_args(argv):
    p = argparse.ArgumentParser(prog="hf_audit.py", description="Audit a repo against the harness ladder.")
    p.add_argument("--repo", help="path to the repo to audit")
    p.add_argument("--json", metavar="OUT", help="write the JSON report to OUT ('-' prints JSON instead of markdown)")
    p.add_argument("--run", action="store_true", help="execute the harness gates, smoke and workflow-lint scripts")
    p.add_argument("--rung", type=int, choices=range(1, 6), metavar="N",
                   help="evaluate and report only rungs 1..N")
    p.add_argument("--append-ledger", action="store_true",
                   help="append one audit line to <repo>/.harness/ledger.jsonl")
    p.add_argument("--offline", action="store_true", help="skip the one network call (branch protection)")
    p.add_argument("--timeout", type=int, default=900, metavar="SECONDS",
                   help="timeout per script under --run (default 900)")
    p.add_argument("--explain", metavar="CHECK_ID", help="explain one check and exit")
    p.add_argument("--agents", action="store_true",
                   help="print the per-agent capability table (markdown) and exit")
    p.add_argument("--version", action="version", version="harness-factory %s" % HF_VERSION)
    return p.parse_args(argv)


def append_ledger(root: str, rep: dict) -> str:
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "audit",
        "rung_achieved": rep["rung_achieved"],
        "next_rung": rep["next_rung"],
        "failing": [c["id"] for c in rep["checks"] if c["status"] not in (PASS, NA)],
        "hf_version": HF_VERSION,
    }
    d = os.path.join(root, ".harness")
    path = os.path.join(d, "ledger.jsonl")
    # Refuse to write through a symlink that leaves the repo.
    real = os.path.realpath(path)
    if not (real == root or real.startswith(root + os.sep)):
        raise RuntimeError("refusing to write ledger outside the repo: %s" % real)
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.agents:
        from hf_checks import load_checks
        from hf_checks.agents import capability_table
        load_checks()
        sys.stdout.write(capability_table())
        return 0
    if args.explain:
        text = explain(args.explain)
        if text is None:
            from hf_checks.registry import all_checks
            sys.stderr.write("unknown check id %s; known ids:\n%s\n"
                             % (args.explain, " ".join(c.id for c in all_checks())))
            return 2
        sys.stdout.write(text)
        return 0
    if not args.repo:
        sys.stderr.write("hf_audit.py: --repo is required (or use --explain ID)\n")
        return 2
    if not os.path.isdir(args.repo):
        sys.stderr.write("hf_audit.py: not a directory: %s\n" % args.repo)
        return 2
    max_rung = args.rung or 5
    ctx = RepoContext(args.repo, run=args.run, offline=args.offline, run_timeout=args.timeout)
    rep = build_report(ctx, max_rung)
    payload = json.dumps(rep, indent=2, sort_keys=False)
    if args.json == "-":
        sys.stdout.write(payload + "\n")
    else:
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
        sys.stdout.write(render_markdown(rep, ctx, max_rung))
    if args.append_ledger:
        path = append_ledger(ctx.root, rep)
        sys.stderr.write("ledger: appended to %s\n" % path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
    except Exception as e:  # tool error, not a finding
        sys.stderr.write("hf_audit.py: error: %s: %s\n" % (e.__class__.__name__, e))
        sys.exit(2)
