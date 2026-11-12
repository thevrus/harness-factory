#!/usr/bin/env python3
"""Collect maintainer corrections on agent pull requests into .harness/rules-inbox.md.

Reads "changes requested" reviews and review comments on PRs labeled
agent:authored, keeps only those written by logins in .harness/maintainers.txt
(learning from any commenter would be an injection path), groups them by a
failure signature from .harness/failure-signatures.yml (keyword match) or by
a normalized first line, and rewrites the managed block of the inbox with
counts and links. It proposes only. Promotion to a check or rule happens in a
human-reviewed pull request once a candidate has 2 or more independent
occurrences (distinct PRs).

Usage: mine-corrections.py [--repo OWNER/NAME] [--days 30] [--inbox PATH]
                           [--dump F] [--from-file F] [--dry-run]
Exit 0 ok, 2 setup error.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

AGENT_LABEL = "agent:authored"
START = "<!-- mine-corrections:start -->"
END = "<!-- mine-corrections:end -->"


def die(msg):
    sys.stderr.write("mine-corrections: MISCONFIGURED: %s\n" % msg)
    sys.exit(2)


def gh(path, params=None):
    cmd = ["gh", "api", "-X", "GET", path]
    for k, v in (params or {}).items():
        cmd += ["-f", "%s=%s" % (k, v)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        die("gh api %s timed out after 60s" % path)
    if r.returncode != 0:
        die("gh api %s failed: %s" % (path, r.stderr.strip()[:300]))
    return json.loads(r.stdout or "null")


def pages(path, params=None):
    out, page = [], 1
    while True:
        data = gh(path, dict(params or {}, per_page="100", page=str(page)))
        items = data.get("items", []) if isinstance(data, dict) else data
        out += items
        if len(items) < 100 or len(out) >= 1000:
            return out
        page += 1


def read_list(path):
    if not os.path.isfile(path):
        die("%s not found" % path)
    with open(path, encoding="utf-8") as f:
        return [ln.split("#", 1)[0].strip() for ln in f if ln.split("#", 1)[0].strip()]


def read_signatures(path):
    """Parse the constrained format of failure-signatures.yml (see its header)."""
    sigs, cur = [], None
    if not os.path.isfile(path):
        return sigs
    with open(path, encoding="utf-8") as f:
        for ln in f:
            m = re.match(r"^- id:\s*([\w-]+)\s*$", ln)
            if m:
                cur = {"id": m.group(1), "keywords": []}
                sigs.append(cur)
                continue
            m = re.match(r"^\s+keywords:\s*\[(.*)\]\s*$", ln)
            if m and cur is not None:
                cur["keywords"] = [k.strip().strip("\"'").lower() for k in m.group(1).split(",") if k.strip()]
    return sigs


def classify(text, sigs):
    low = text.lower()
    for s in sigs:
        if any(k and k in low for k in s["keywords"]):
            return s["id"]
    first = re.sub(r"[^a-z0-9 ]+", "", low.strip().splitlines()[0] if low.strip() else "")
    return "text:" + " ".join(first.split()[:8])


def fetch(repo, days):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    prs = pages("search/issues", {"q": 'repo:%s is:pr label:"%s" updated:>=%s' % (repo, AGENT_LABEL, since)})
    data = {"repo": repo, "since": since, "prs": []}
    for p in prs:
        n = p["number"]
        data["prs"].append({"number": n, "url": p.get("html_url", ""),
                            "reviews": pages("repos/%s/pulls/%d/reviews" % (repo, n)),
                            "comments": pages("repos/%s/pulls/%d/comments" % (repo, n))})
    return data


def collect(data, maintainers, sigs):
    allowed = {m.lower() for m in maintainers}
    groups = {}
    for pr in data["prs"]:
        notes = [(r.get("body") or "", r.get("html_url") or pr["url"], (r.get("user") or {}).get("login", ""))
                 for r in pr["reviews"] if r.get("state") == "CHANGES_REQUESTED"]
        notes += [(c.get("body") or "", c.get("html_url") or pr["url"], (c.get("user") or {}).get("login", ""))
                  for c in pr["comments"]]
        for body, url, login in notes:
            if login.lower() not in allowed or not body.strip():
                continue
            key = classify(body, sigs)
            g = groups.setdefault(key, {"prs": set(), "links": [], "excerpt": ""})
            g["prs"].add(pr["number"])
            g["links"].append(url)
            if not g["excerpt"]:
                g["excerpt"] = re.sub(r"\s+", " ", re.sub(r"<!--.*?-->", "", body, flags=re.S))[:160]
    return groups


def render(groups, data):
    lines = [START, "", "Generated %s from maintainer reviews on agent PRs updated since %s." % (
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"), data["since"]), ""]
    if not groups:
        lines.append("No maintainer corrections found.")
    for key, g in sorted(groups.items(), key=lambda kv: -len(kv[1]["prs"])):
        n = len(g["prs"])
        flag = "PROMOTION CANDIDATE (>=2 independent PRs)" if n >= 2 else "watch"
        lines.append("### %s: %d PR(s), %d comment(s), %s" % (key, n, len(g["links"]), flag))
        lines.append("")
        lines.append("> " + g["excerpt"].replace("@", "`@`"))
        lines.append("")
        for url in g["links"][:10]:
            lines.append("- %s" % url)
        lines.append("")
    lines.append(END)
    return "\n".join(lines) + "\n"


def write_inbox(path, block):
    text = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
    if START in text and END in text:
        text = text[:text.index(START)] + block + text[text.index(END) + len(END):].lstrip("\n")
    else:
        text = text.rstrip("\n") + "\n\n" + block
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser(description="Collect maintainer corrections on agent PRs (proposes only).")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--inbox", default=".harness/rules-inbox.md")
    ap.add_argument("--maintainers", default=".harness/maintainers.txt")
    ap.add_argument("--signatures", default=".harness/failure-signatures.yml")
    ap.add_argument("--dump")
    ap.add_argument("--from-file")
    ap.add_argument("--dry-run", action="store_true", help="print the inbox block instead of writing it")
    args = ap.parse_args()
    maintainers = read_list(args.maintainers)
    if not maintainers:
        die("%s lists no logins; refusing to learn from anyone" % args.maintainers)
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        if not shutil.which("gh"):
            die("gh CLI not found")
        if not args.repo:
            die("pass --repo OWNER/NAME")
        data = fetch(args.repo, args.days)
        if args.dump:
            with open(args.dump, "w", encoding="utf-8") as f:
                json.dump(data, f)
    block = render(collect(data, maintainers, read_signatures(args.signatures)), data)
    if args.dry_run:
        sys.stdout.write(block)
    else:
        write_inbox(args.inbox, block)
        print("mine-corrections: updated %s" % args.inbox)
    return 0


if __name__ == "__main__":
    sys.exit(main())
