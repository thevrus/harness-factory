#!/usr/bin/env python3
"""Factory metrics for a time window, computed from GitHub via `gh api`.

Agent pull requests are those labeled agent:authored (set by the implement
lane). Every metric prints its numerator and denominator and a human-PR
baseline where one exists. Missing data is null with a reason, never 0.
Automation rate is always reported together with the revert rate.

Usage:
  metrics.py [--repo OWNER/NAME] [--window-days 7] [--json-out F]
             [--findings-out F] [--ledger PATH | --no-ledger]
             [--dump F] [--from-file F] [--dry-run]

--dump saves the raw API data; --from-file computes from such a dump with no
network. --findings-out writes one regression finding for agent-monitor.yml
(or nothing when there is none). Exit 0 ok, 2 setup error (gh missing etc.).
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys

AGENT_LABEL = "agent:authored"
READY_LABEL = "agent:ready"
REVERT_DAYS = 14
BASELINE_SAMPLE = 50


def die(msg):
    sys.stderr.write("metrics: MISCONFIGURED: %s\n" % msg)
    sys.exit(2)


def gh(path, params=None):
    cmd = ["gh", "api", "-X", "GET", path]
    for k, v in (params or {}).items():
        cmd += ["-f", "%s=%s" % (k, v)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("gh api %s timed out after 60s" % path)
    if r.returncode != 0:
        raise RuntimeError("gh api %s failed: %s" % (path, r.stderr.strip()[:300]))
    return json.loads(r.stdout or "null")


def gh_pages(path, params=None, cap=1000):
    out, page = [], 1
    while len(out) < cap:
        p = dict(params or {}, per_page="100", page=str(page))
        data = gh(path, p)
        items = data.get("items", []) if isinstance(data, dict) else data
        out += items
        if len(items) < 100:
            break
        page += 1
    return out


def parse_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) if s else None


def is_bot(login):
    return not login or login.endswith("[bot]")


def labels_of(item):
    return {lb["name"] for lb in item.get("labels", [])}


# ------------------------------------------------------------------ fetching
def fetch(repo, since, dry):
    s = since.strftime("%Y-%m-%d")
    searches = {
        "merged": "repo:%s is:pr is:merged merged:>=%s" % (repo, s),
        "closed_unmerged": "repo:%s is:pr is:closed is:unmerged closed:>=%s" % (repo, s),
        "reverts": 'repo:%s is:pr is:merged "Revert" in:title merged:>=%s' % (repo, s),
    }
    if dry:
        for k, q in searches.items():
            print("would call: gh api -X GET search/issues -f q='%s'" % q)
        print("would call, per agent PR: pulls/N, pulls/N/reviews, pulls/N/commits, issues/N/comments, issues/N/timeline")
        print("would call: repos/%s, commits/<default>/check-runs" % repo)
        sys.exit(0)
    data = {"repo": repo, "since": s, "fetched_at": now_iso()}
    for k, q in searches.items():
        data[k] = gh_pages("search/issues", {"q": q})
    agent = [i for i in data["merged"] + data["closed_unmerged"] if AGENT_LABEL in labels_of(i)]
    human = [i for i in data["merged"] if AGENT_LABEL not in labels_of(i)][:BASELINE_SAMPLE]
    data["details"] = {}
    for item in agent + human:
        n = item["number"]
        d = {"pull": gh("repos/%s/pulls/%d" % (repo, n)),
             "reviews": gh_pages("repos/%s/pulls/%d/reviews" % (repo, n)),
             "commits": gh_pages("repos/%s/pulls/%d/commits" % (repo, n)),
             "comments": gh_pages("repos/%s/issues/%d/comments" % (repo, n)),
             "timeline": gh_pages("repos/%s/issues/%d/timeline" % (repo, n))}
        m = re.search(r"(?i)\bclose[sd]?\s+#(\d+)", d["pull"].get("body") or "")
        if m and AGENT_LABEL in labels_of(item):
            d["issue_timeline"] = gh_pages("repos/%s/issues/%s/timeline" % (repo, m.group(1)))
        data["details"][str(n)] = d
    try:
        default = gh("repos/%s" % repo)["default_branch"]
        data["default_branch"] = default
        data["default_checks"] = gh("repos/%s/commits/%s/check-runs" % (repo, default), {"per_page": "100"})
        head = data["default_checks"]["check_runs"][0]["head_sha"] if data["default_checks"]["check_runs"] else None
        data["default_head_pulls"] = gh("repos/%s/commits/%s/pulls" % (repo, head)) if head else []
    except (RuntimeError, KeyError, IndexError) as e:
        data["default_checks_error"] = str(e)
    return data


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------- computing
def ratio(num, den, reason_if_empty):
    if not den:
        return {"value": None, "numerator": num, "denominator": den, "reason": reason_if_empty}
    return {"value": round(num / den, 4), "numerator": num, "denominator": den}


def ready_time(d):
    for ev in d.get("timeline", []):
        if ev.get("event") == "ready_for_review":
            return parse_ts(ev.get("created_at"))
    return None


def human_touches(d):
    reviews = [r for r in d["reviews"] if not is_bot((r.get("user") or {}).get("login"))]
    commits = [c for c in d["commits"] if not is_bot((c.get("author") or {}).get("login"))]
    comments = [c for c in d["comments"] if not is_bot((c.get("user") or {}).get("login"))]
    return len(reviews) + len(commits) + len(comments), commits


def first_pass(d):
    if any(r.get("state") == "CHANGES_REQUESTED" for r in d["reviews"]):
        return False
    _, commits = human_touches(d)
    rt = ready_time(d)
    late = [c for c in commits if rt is None or (parse_ts(c["commit"]["committer"]["date"]) or rt) >= rt]
    return not late


def reverted(n, merge_sha, reverts):
    for r in reverts:
        body = (r.get("body") or "") + " " + (r.get("title") or "")
        if re.search(r"#%d\b" % n, body) or (merge_sha and merge_sha[:7] in body):
            return r["number"]
    return None


def hours(a, b):
    return round((b - a).total_seconds() / 3600, 2) if a and b else None


def median_or_null(vals, reason):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"value": None, "n": 0, "reason": reason}
    return {"value": round(statistics.median(vals), 2), "n": len(vals), "unit": "hours (median)"}


def repeat_failures(since):
    cases = []
    for sig_file in glob.glob(".harness/failures/*/signature"):
        name = os.path.basename(os.path.dirname(sig_file))
        try:
            day = dt.datetime.strptime(name[:10], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        with open(sig_file, encoding="utf-8") as f:
            cases.append((day, f.read().strip()))
    in_window = [c for c in cases if c[0] >= since]
    if not in_window:
        return {"value": None, "numerator": 0, "denominator": 0,
                "reason": "no failure cases recorded in .harness/failures/ for this window"}
    rep = sum(1 for day, sig in in_window
              if any(s == sig and day - dt.timedelta(days=30) <= d2 < day for d2, s in cases))
    return ratio(rep, len(in_window), "")


def compute(data):
    since = parse_ts(data["since"] + "T00:00:00Z")
    det = data["details"]
    merged = data["merged"]
    agent_merged = [i for i in merged if AGENT_LABEL in labels_of(i)]
    agent_unmerged = [i for i in data["closed_unmerged"] if AGENT_LABEL in labels_of(i)]
    human_merged = [i for i in merged if AGENT_LABEL not in labels_of(i)]
    human_sample = [i for i in human_merged if str(i["number"]) in det]

    def rev_rate(items):
        hits = []
        for i in items:
            d = det.get(str(i["number"]))
            sha = d["pull"].get("merge_commit_sha") if d else None
            r = reverted(i["number"], sha, data["reverts"])
            if r:
                hits.append({"pr": i["number"], "reverted_by": r})
        return hits

    agent_reverts = rev_rate(agent_merged)
    human_reverts = rev_rate(human_sample)
    m = {}
    m["automation"] = {
        "automation_rate": ratio(len(agent_merged), len(merged), "no merged PRs in window"),
        "revert_rate_agent": ratio(len(agent_reverts), len(agent_merged), "no merged agent PRs in window"),
        "note": "revert observation is right-censored: PRs merged late in the window had less than %d days to be reverted" % REVERT_DAYS,
    }
    m["revert_rate"] = {"agent": m["automation"]["revert_rate_agent"],
                        "human_baseline": ratio(len(human_reverts), len(human_sample), "no human PRs sampled"),
                        "reverted_agent_prs": agent_reverts}
    closed_agent = agent_merged + agent_unmerged
    fp = sum(1 for i in agent_merged if first_pass(det[str(i["number"])]))
    fp_h = sum(1 for i in human_sample if first_pass(det[str(i["number"])]))
    m["first_pass_acceptance"] = {
        "agent": ratio(fp, len(closed_agent), "no closed agent PRs in window"),
        "human_baseline": ratio(fp_h, len(human_sample), "no human PRs sampled"),
        "denominator_note": "agent denominator = merged + closed-unmerged agent PRs",
    }
    touches = [human_touches(det[str(i["number"])])[0] for i in closed_agent]
    m["human_touch_count"] = ({"value": round(sum(touches) / len(touches), 2), "total": sum(touches),
                               "denominator": len(touches), "unit": "human reviews+commits+comments per agent PR"}
                              if touches else {"value": None, "reason": "no closed agent PRs in window"})
    cyc = []
    for i in agent_merged:
        d = det[str(i["number"])]
        labeled = [parse_ts(e.get("created_at")) for e in d.get("issue_timeline", [])
                   if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == READY_LABEL]
        cyc.append(hours(labeled[-1], parse_ts(d["pull"].get("merged_at"))) if labeled else None)
    m["cycle_time"] = {
        "agent_ready_to_merge": median_or_null(cyc, "no merged agent PR links an issue with an agent:ready label event"),
        "human_baseline_open_to_merge": median_or_null(
            [hours(parse_ts(det[str(i["number"])]["pull"].get("created_at")),
                   parse_ts(det[str(i["number"])]["pull"].get("merged_at"))) for i in human_sample],
            "no human PRs sampled"),
    }
    m["repeat_failure_rate"] = repeat_failures(since)
    costs = []
    for i in closed_agent:
        mm = re.search(r"(?m)^Agent-Cost-USD:\s*([0-9.]+)\s*$", det[str(i["number"])]["pull"].get("body") or "")
        if mm:
            costs.append(float(mm.group(1)))
    if not costs:
        m["cost_per_merged_agent_pr"] = {"value": None, "reason": "no Agent-Cost-USD lines on agent PRs in window"}
    elif not agent_merged:
        m["cost_per_merged_agent_pr"] = {"value": None, "total_usd": round(sum(costs), 4), "reason": "no merged agent PRs"}
    else:
        m["cost_per_merged_agent_pr"] = {"value": round(sum(costs) / len(agent_merged), 4), "total_usd": round(sum(costs), 4),
                                         "denominator": len(agent_merged),
                                         "note": "counts closed-unmerged PRs' cost; runs that opened no PR are not counted",
                                         "prs_with_cost": len(costs), "agent_prs_closed": len(closed_agent)}
    m["counts"] = {"merged": len(merged), "agent_merged": len(agent_merged),
                   "agent_closed_unmerged": len(agent_unmerged), "human_merged": len(human_merged),
                   "human_baseline_sample": len(human_sample)}
    return m


def find_regression(data, m):
    items = []
    for r in m["revert_rate"]["reverted_agent_prs"]:
        items.append("Agent PR #%d was reverted by #%d." % (r["pr"], r["reverted_by"]))
    checks = (data.get("default_checks") or {}).get("check_runs", [])
    failing = sorted({c["name"] for c in checks if c.get("conclusion") in ("failure", "timed_out")})
    if failing:
        agent_head = any(AGENT_LABEL in labels_of(p) for p in data.get("default_head_pulls", []))
        items.append("Default branch `%s` head has failing checks (%d)%s." % (
            data.get("default_branch", "?"), len(failing), "; its last PR is agent-authored" if agent_head else ""))
    if not items:
        return None
    key = hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()[:16]
    body = "Regressions found by the monitor:\n\n" + "\n".join("- " + i for i in items)
    return {"key": key, "title": "Factory regression: %d item(s)" % len(items), "body": body}


def fmt(x):
    if not isinstance(x, dict):
        return str(x)
    if x.get("value") is None:
        return "null (%s)" % x.get("reason", "no data")
    if "denominator" in x and "numerator" in x:
        return "%s (%s/%s)" % (x["value"], x["numerator"], x["denominator"])
    return "%s (n=%s)" % (x["value"], x.get("n", x.get("denominator", "?")))


def report(m, repo, days):
    a = m["automation"]
    print("Factory metrics for %s, last %d days" % (repo, days))
    print("  automation rate        %s   | revert rate (agent) %s" % (fmt(a["automation_rate"]), fmt(a["revert_rate_agent"])))
    print("  revert rate, human     %s" % fmt(m["revert_rate"]["human_baseline"]))
    print("  first-pass acceptance  agent %s | human %s" % (fmt(m["first_pass_acceptance"]["agent"]), fmt(m["first_pass_acceptance"]["human_baseline"])))
    print("  human touch count      %s" % fmt(m["human_touch_count"]))
    print("  cycle time             agent %s | human %s" % (fmt(m["cycle_time"]["agent_ready_to_merge"]), fmt(m["cycle_time"]["human_baseline_open_to_merge"])))
    print("  repeat-failure rate    %s" % fmt(m["repeat_failure_rate"]))
    print("  cost per merged PR     %s" % fmt(m["cost_per_merged_agent_pr"]))
    print("  counts                 %s" % json.dumps(m["counts"]))


def main():
    ap = argparse.ArgumentParser(description="Factory metrics from GitHub (needs gh, authenticated).")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--json-out")
    ap.add_argument("--findings-out")
    ap.add_argument("--ledger", default=".harness/ledger.jsonl")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--dump")
    ap.add_argument("--from-file")
    ap.add_argument("--dry-run", action="store_true", help="print the API calls and exit")
    args = ap.parse_args()
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            data = json.load(f)
        repo = data.get("repo", args.repo or "?")
    else:
        if not shutil.which("gh") and not args.dry_run:
            die("gh CLI not found")
        repo = args.repo
        if not repo and not args.dry_run:
            r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], capture_output=True, text=True)
            repo = r.stdout.strip()
        if not repo:
            repo = "OWNER/NAME" if args.dry_run else die("cannot determine repository; pass --repo")
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.window_days)
        try:
            data = fetch(repo, since, args.dry_run)
        except RuntimeError as e:
            die(str(e))
        if args.dump:
            with open(args.dump, "w", encoding="utf-8") as f:
                json.dump(data, f)
    m = compute(data)
    report(m, repo, args.window_days)
    record = {"ts": now_iso(), "kind": "metrics", "repo": repo, "window_days": args.window_days,
              "since": data["since"], "metrics": m}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    if args.findings_out:
        finding = find_regression(data, m)
        if finding:
            with open(args.findings_out, "w", encoding="utf-8") as f:
                json.dump(finding, f)
    if not args.no_ledger:
        os.makedirs(os.path.dirname(args.ledger) or ".", exist_ok=True)
        with open(args.ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
