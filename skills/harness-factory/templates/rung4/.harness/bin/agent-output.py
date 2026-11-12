#!/usr/bin/env python3
"""Validate and render structured agent output before a write job publishes it.

The agent lane never writes to GitHub. It emits JSON; this script, run by a
separate job that holds the write token and no model key, checks the JSON
against a schema in .harness/schemas/, enforces caps and allowlists, neutralizes
markdown that could ping people, load remote images or hide text, and renders
the exact text to publish. Anything invalid fails closed (exit 1) and nothing
is published.

Subcommands:
  review    --input F --changed-files F --out BODY.md
  triage    --input F --allowlist F --max-labels N --labels-out F --comment-out F
  implement --input F --out SUMMARY.md
  incident  --input F --title-out F --body-out F

Common: --schema PATH (default .harness/schemas/<kind>.schema.json),
        --run-url URL (written as the "Agent-Run:" attribution line).
Exit codes: 0 valid, 1 invalid output (refuse to publish), 2 usage or setup error.
"""
import argparse
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_INPUT_BYTES = 256 * 1024
MAX_BODY_CHARS = 60000  # GitHub rejects bodies above 65536 characters.


class Invalid(Exception):
    pass


def _load_stripper():
    path = os.path.join(HERE, "strip-hidden.py")
    if not os.path.isfile(path):
        sys.stderr.write("agent-output: MISCONFIGURED: %s is missing\n" % path)
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("strip_hidden", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


STRIP = _load_stripper()


# ---------------------------------------------------------------- schema check
def check(value, schema, where="$"):
    """Validate the JSON Schema subset used by .harness/schemas/*.json."""
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise Invalid("%s: expected integer" % where)
        if "minimum" in schema and value < schema["minimum"]:
            raise Invalid("%s: below minimum %s" % (where, schema["minimum"]))
    elif t in types:
        if not isinstance(value, types[t]):
            raise Invalid("%s: expected %s" % (where, t))
    if "enum" in schema and value not in schema["enum"]:
        raise Invalid("%s: %r not in %s" % (where, value, schema["enum"]))
    if t == "string" and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise Invalid("%s: longer than %d characters" % (where, schema["maxLength"]))
    if t == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise Invalid("%s: more than %d items" % (where, schema["maxItems"]))
        for i, item in enumerate(value):
            check(item, schema.get("items", {}), "%s[%d]" % (where, i))
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise Invalid("%s: missing required key %r" % (where, key))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise Invalid("%s: unexpected keys %s" % (where, extra))
        for key, sub in props.items():
            if key in value:
                check(value[key], sub, "%s.%s" % (where, key))


def load_input(path, schema_path):
    try:
        st = os.lstat(path)
    except OSError:
        raise Invalid("input %s does not exist" % path)
    if not os.path.isfile(path) or os.path.islink(path):
        raise Invalid("input must be a regular file, not a link")
    if st.st_size > MAX_INPUT_BYTES:
        raise Invalid("input is %d bytes, cap is %d" % (st.st_size, MAX_INPUT_BYTES))
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise Invalid("input is not valid JSON: %s" % e)
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write("agent-output: MISCONFIGURED: cannot read schema %s: %s\n" % (schema_path, e))
        sys.exit(2)
    check(data, schema)
    refuse_credentials(data)
    return data


# ------------------------------------------------------------ credential shapes
# Injected text can ask the agent to copy a secret into its output, which this
# script would then publish. Refuse the whole result if any string looks like
# a credential. The error names the field, never the matched text.
CRED_PREFIX_RE = re.compile(r"sk-ant-|(?<![A-Za-z0-9])gh[pousr]_|github_pat_")
# Base64url runs of 40+ characters. Pure hex (commit SHAs, digests) and runs
# without upper case, lower case and a digit (long identifiers) are allowed.
B64_RUN_RE = re.compile(r"[A-Za-z0-9_-]{40,}")


def _looks_random(run):
    if re.fullmatch(r"[0-9a-fA-F]+", run):
        return False
    return bool(re.search(r"[A-Z]", run) and re.search(r"[a-z]", run) and re.search(r"[0-9]", run))


def refuse_credentials(value, where="$"):
    if isinstance(value, str):
        if CRED_PREFIX_RE.search(value):
            raise Invalid("%s: contains a credential-shaped token prefix" % where)
        if any(_looks_random(m.group(0)) for m in B64_RUN_RE.finditer(value)):
            raise Invalid("%s: contains a credential-shaped run of 40+ base64url characters" % where)
    elif isinstance(value, dict):
        for key, sub in value.items():
            refuse_credentials(sub, "%s.%s" % (where, key))
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            refuse_credentials(sub, "%s[%d]" % (where, i))


# ------------------------------------------------------------ markdown hygiene
TAG_RE = re.compile(r"</?[A-Za-z][^>\n]*>")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
URL_RE = re.compile(r"\b(https?)://([^\s)>\]]+)")
MENTION_RE = re.compile(r"(?<![\w`])@([A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9_.-]+)?)")


def repo_prefix():
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return "%s/%s/" % (server, repo) if repo else None


def safe_md(text):
    """Make agent-written text safe to post: visible, no pings, no remote loads."""
    text, _ = STRIP.clean(text, 0)
    text = IMG_RE.sub("[image removed]", text)
    text = TAG_RE.sub("", text)
    prefix = repo_prefix()

    def link(m):
        label, url = m.group(1), m.group(2)
        if prefix and url.startswith(prefix):
            return "[%s](%s)" % (label, url)
        return label

    text = LINK_RE.sub(link, text)

    def url(m):
        full = m.group(0)
        if prefix and full.startswith(prefix):
            return full
        return "hxxp%s://%s" % ("s" if m.group(1) == "https" else "", m.group(2))

    text = URL_RE.sub(url, text)
    # Mentions inside code spans do not notify anyone.
    text = MENTION_RE.sub(lambda m: "`@%s`" % m.group(1), text)
    return text.strip()


def one_line(text, cap=300):
    return re.sub(r"\s+", " ", safe_md(text))[:cap]


def footer(kind, run_url):
    lines = ["", "---", "agent:%s output, validated by .harness/bin/agent-output.py." % kind]
    if run_url:
        lines.append("Agent-Run: %s" % run_url)
    return "\n".join(lines) + "\n"


def write(path, text):
    if len(text) > MAX_BODY_CHARS:
        raise Invalid("rendered text is %d characters, cap is %d" % (len(text), MAX_BODY_CHARS))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ------------------------------------------------------------------ renderers
PATH_RE = re.compile(r"^[A-Za-z0-9_@+./ -]+$")


def do_review(args):
    data = load_input(args.input, args.schema)
    changed = set()
    with open(args.changed_files, encoding="utf-8") as f:
        changed = {ln.strip() for ln in f if ln.strip()}
    out = ["## Agent review", "", safe_md(data["summary"]) or "(no summary)", ""]
    order = {"high": 0, "medium": 1, "low": 2}
    findings = sorted(data["findings"], key=lambda x: order[x["severity"]])
    if findings:
        out.append("### Findings (%d)" % len(findings))
        out.append("")
        for i, fd in enumerate(findings, 1):
            path = fd["path"]
            if not PATH_RE.match(path) or ".." in path.split("/"):
                raise Invalid("finding %d: path %r has unexpected characters" % (i, path))
            if path not in changed:
                raise Invalid("finding %d: path %r is not in the pull request's changed files" % (i, path))
            loc = "%s:%d" % (path, fd["line"]) if "line" in fd else path
            out.append("%d. **%s** `%s` %s" % (i, fd["severity"].upper(), loc, one_line(fd["title"], 200)))
            out.append("")
            out.append("   " + safe_md(fd["detail"]).replace("\n", "\n   "))
            out.append("")
    else:
        out += ["No findings.", ""]
    mr = data["merge_risk"]
    out += [
        "### Merge risk: %s" % mr["level"].upper(),
        "",
        "Informs the human reviewer only. This review never approves or blocks.",
        "",
        "- Alignment: " + one_line(mr["alignment"]),
        "- Impact: " + one_line(mr["impact"]),
        "- Verification: " + one_line(mr["verification"]),
        "- Rollback cost: " + one_line(mr["rollback_cost"]),
        "- Error detection: " + one_line(mr["error_detection"]),
    ]
    write(args.out, "\n".join(out) + "\n" + footer("review", args.run_url))


WORKFLOW_ONLY_LABELS = ("agent:triage-failed",)


def read_allowlist(path):
    labels = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.split("#", 1)[0].strip()
            if ln:
                labels.append(ln)
    if not labels:
        sys.stderr.write("agent-output: MISCONFIGURED: allowlist %s is empty\n" % path)
        sys.exit(2)
    return labels


def do_triage(args):
    data = load_input(args.input, args.schema)
    allow = read_allowlist(args.allowlist)
    labels = []
    for lb in data["labels"]:
        if lb in WORKFLOW_ONLY_LABELS:
            # Set by the workflow itself when a result is rejected; the agent
            # must never be able to choose it, even if someone allowlists it.
            raise Invalid("label %r is reserved for the workflow" % lb)
        if lb not in allow:
            # A label outside the allowlist is a sign of injection or drift:
            # refuse the whole result rather than apply part of it.
            raise Invalid("label %r is not in %s" % (lb, args.allowlist))
        if lb not in labels:
            labels.append(lb)
    if len(labels) > args.max_labels:
        raise Invalid("%d labels requested, cap is %d" % (len(labels), args.max_labels))
    comment = safe_md(data["comment"])
    write(args.labels_out, "".join(lb + "\n" for lb in labels))
    body = comment if comment else "Triage applied labels: %s" % (", ".join(labels) or "none")
    write(args.comment_out, body + "\n" + footer("triage", args.run_url))


def do_implement(args):
    data = load_input(args.input, args.schema)
    out = ["### What the agent says it did", "", safe_md(data["summary"]) or "(no summary)", ""]
    out.append("Fix attempts used: %d" % data["attempts"])
    if data["blocked"]:
        out += ["", "### Blocked", "", safe_md(data.get("blocker", "")) or "(no reason given)"]
    write(args.out, "\n".join(out) + "\n")


def do_incident(args):
    data = load_input(args.input, args.schema)
    title = one_line(data["title"], 120)
    if not title:
        raise Invalid("empty title")
    write(args.title_out, title + "\n")
    write(args.body_out, safe_md(data["body"]) + "\n\nMonitor-Key: %s\n" % data["key"] + footer("monitor", args.run_url))


def main():
    ap = argparse.ArgumentParser(description="Validate and render structured agent output.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, kind):
        p.add_argument("--input", required=True)
        p.add_argument("--schema", default=".harness/schemas/%s.schema.json" % kind)
        p.add_argument("--run-url", default="")
        return p

    p = common(sub.add_parser("review"), "review")
    p.add_argument("--changed-files", required=True)
    p.add_argument("--out", required=True)
    p = common(sub.add_parser("triage"), "triage")
    p.add_argument("--allowlist", default=".harness/triage-labels.txt")
    p.add_argument("--max-labels", type=int, default=2)
    p.add_argument("--labels-out", required=True)
    p.add_argument("--comment-out", required=True)
    p = common(sub.add_parser("implement"), "implement")
    p.add_argument("--out", required=True)
    p = common(sub.add_parser("incident"), "incident")
    p.add_argument("--title-out", required=True)
    p.add_argument("--body-out", required=True)
    args = ap.parse_args()
    handler = {"review": do_review, "triage": do_triage, "implement": do_implement, "incident": do_incident}[args.cmd]
    try:
        handler(args)
    except Invalid as e:
        sys.stderr.write("agent-output: INVALID (%s): %s\n" % (args.cmd, e))
        return 1
    print("agent-output: VALID (%s)" % args.cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
