#!/usr/bin/env python3
"""Lint GitHub Actions workflows for unsafe agent and injection patterns.

Usage: lint-agent-workflows.py [--root DIR] [--strict] [FILE ...]

Output: one line per finding, "file:line rule message", then a final line
"HARNESS_WORKFLOW_LINT: PASS|FAIL (...)". Exit 0 on PASS, 1 on FAIL.

YAML parsing: the standard library has no YAML parser, so this uses a careful
line-based scanner for block-style YAML (mappings, sequences, "|"/">" block
scalars, quoted keys, comments), which is how workflows are written in
practice. It does not interpret flow collections ({...} / [...]) beyond
treating them as text, anchors/aliases, or multi-document files. Anything it
cannot scan is reported as a finding (fail closed), never skipped silently.

Rule scope:
  every workflow   event-in-run, pwn-request-checkout, pin-placeholder,
                   model-secret-in-write-job (also non-literal secrets
                   access: toJSON(secrets), secrets[expr], secrets.*),
                   secret-to-local-call (write job passing secrets to a
                   local reusable workflow or composite action)
  agent workflows  missing-permissions, write-all, unpinned-uses,
                   event-in-agent-input, token-to-agent, missing-timeout,
                   missing-kill-switch, agent-job-write-scope,
                   extra-secret-in-agent-job, plus the vendor rules below
                   (claude: unrestricted-bash, which also covers wildcards
                   on interpreters, wrappers, runners and git/gh;
                   permission-bypass)

Two-lane split: a job that runs an agent holds no write scope (id-token
excepted, for OIDC model auth), and a job that holds a write scope never sees
a model credential. Effective permissions are the job's own block, else the
workflow's top-level block.
  --strict         applies the agent-workflow rules to every workflow

An "agent workflow" uses a known agent action or invokes a known agent CLI
(see VENDORS). Vendor rules apply only to steps of that vendor, because flag
and input names differ per vendor. Vendors without verified rules get only
the generic agent rules until their flags are confirmed from their docs.
"""

import argparse
import os
import re
import sys

# --------------------------------------------------------------------------
# vendors
# --------------------------------------------------------------------------

VENDORS = {
    "claude": {
        "actions": ["anthropics/claude-code-action", "anthropics/claude-code-base-action"],
        "cli": r"(?:claude|@anthropic-ai/claude-code(?:@\S+)?)",
    },
    "codex": {
        "actions": ["openai/codex-action"],
        "cli": r"(?:codex|@openai/codex(?:@\S+)?)\s+exec",
    },
    "gemini": {
        "actions": ["google-github-actions/run-gemini-cli"],
        "cli": r"(?:gemini|@google/gemini-cli(?:@\S+)?)",
    },
    "cursor": {"actions": [], "cli": r"cursor-agent"},
    "opencode": {"actions": [], "cli": r"(?:opencode|opencode-ai(?:@\S+)?)"},
}

# A command word: start of line or after a shell separator, optionally behind
# env assignments and launchers (npx, bunx, pnpm dlx, sudo, timeout N).
_CMD_PREFIX = (r"(?:^|[;&|`(]|\$\()\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
               r"(?:(?:sudo|nohup|exec)\s+|timeout\s+\S+\s+|npx\s+(?:-y\s+|--yes\s+)?"
               r"|bunx\s+|pnpm\s+dlx\s+|yarn\s+dlx\s+)*(?:[\w.~/-]*/)?")
CLI_RX = {v: re.compile(_CMD_PREFIX + spec["cli"] + r"(?=\s|$|;|\))")
          for v, spec in VENDORS.items()}

EXPR_RX = re.compile(r"\$\{\{(.*?)\}\}")
UNTRUSTED_RX = re.compile(r"\bgithub\.event\b|\bgithub\.head_ref\b")
SHA_RX = re.compile(r"^[0-9a-f]{40}$")
PLACEHOLDER = "PIN_SHA_BEFORE_USE"
KILL_SWITCH = "vars.AGENT_ENABLED"
# Model credentials: named here, plus any secret whose name starts with a
# model vendor prefix. A write-scoped job must reference none of them.
MODEL_SECRETS = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY",
                 "GEMINI_API_KEY", "GOOGLE_API_KEY", "CURSOR_API_KEY"}
MODEL_SECRET_RX = re.compile(r"^(?:ANTHROPIC|CLAUDE|OPENAI|CODEX|GEMINI|AZURE_OPENAI|CURSOR|OPENROUTER)_")
SECRET_REF_RX = re.compile(r"secrets\.([A-Za-z0-9_]+)|secrets\[\s*['\"]([A-Za-z0-9_]+)['\"]\s*\]")
# A use of the secrets context that is not a literal name: toJSON(secrets),
# secrets[matrix.k], secrets[format(...)], secrets.* (object filter). It can
# expose every secret, model keys included, so the linter cannot tell which.
DYNAMIC_SECRETS_RX = re.compile(
    r"(?<![\w.-])secrets\b(?!\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)(?!\s*\[\s*['\"][A-Za-z0-9_]+['\"]\s*\])")
WRITE_OK_FOR_AGENT = {"id-token"}
PR_HEAD_RX = re.compile(r"github\.event\.pull_request\.head|github\.head_ref|"
                        r"github\.event\.workflow_run\.head|refs/pull/|\.head\.(?:sha|ref)\b")


# --------------------------------------------------------------------------
# block-YAML scanner
# --------------------------------------------------------------------------

class ScanError(Exception):
    def __init__(self, line, msg):
        Exception.__init__(self, msg)
        self.line = line


class Scalar(object):
    def __init__(self, text, line, block=None):
        self.text = text            # inline text ("" for block scalars)
        self.line = line
        self.block = block or []    # [(lineno, raw text)] for "|" / ">" bodies

    def lines(self):
        """All (lineno, text) pairs this scalar covers."""
        if self.block:
            return list(self.block)
        return [(self.line, self.text)]

    def value(self):
        if self.block:
            return "\n".join(t for _, t in self.block)
        return unquote(self.text)


class Map(object):
    def __init__(self, line):
        self.line = line
        self.items = []             # [(key, keyline, node)]

    def get(self, key):
        for k, _, node in self.items:
            if k == key:
                return node
        return None

    def keyline(self, key):
        for k, ln, _ in self.items:
            if k == key:
                return ln
        return None


class Seq(object):
    def __init__(self, line):
        self.line = line
        self.items = []


def unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def strip_comment(s):
    """Remove a trailing " # comment" that is not inside quotes."""
    out, quote = [], None
    for i, c in enumerate(s):
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"" and (i == 0 or s[i - 1] in " \t:-[{,"):
            quote = c
        elif c == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        out.append(c)
    return "".join(out).rstrip()


KEY_RX = re.compile(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s'"#][^:#]*?)\s*:(?:\s+(.*))?$""")


class Scanner(object):
    def __init__(self, text):
        self.raw = text.splitlines()
        self.lines = []  # (lineno, indent, content) for significant lines
        for no, line in enumerate(self.raw, 1):
            if "\t" in line[:len(line) - len(line.lstrip())]:
                raise ScanError(no, "tab indentation")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped in ("---", "...") and no > 1 and self.lines:
                raise ScanError(no, "multi-document YAML is not supported")
            if stripped == "---":
                continue
            self.lines.append((no, len(line) - len(line.lstrip(" ")), line.strip()))

    def parse(self):
        if not self.lines:
            return Map(1)
        node, i = self.block(0, self.lines[0][1])
        if i < len(self.lines):
            raise ScanError(self.lines[i][0], "unexpected indentation")
        return node

    def block(self, i, indent):
        content = self.lines[i][2]
        if content == "-" or content.startswith("- "):
            return self.seq(i, indent)
        return self.mapping(i, indent)

    def block_scalar(self, keyline_no, parent_indent):
        """Collect raw lines after keyline_no indented deeper than parent."""
        body, no = [], keyline_no
        while no < len(self.raw):
            line = self.raw[no]
            ind = len(line) - len(line.lstrip(" "))
            if line.strip() and ind <= parent_indent:
                break
            body.append((no + 1, line))
            no += 1
        while body and not body[-1][1].strip():
            body.pop()
        # skip the significant lines the body consumed
        return body, no

    def skip_to_line(self, i, raw_no):
        while i < len(self.lines) and self.lines[i][0] <= raw_no:
            i += 1
        return i

    def value_after_key(self, i, indent, rest, no):
        """Parse the value of a key whose line is self.lines[i]."""
        rest = strip_comment(rest or "")
        if rest[:1] in ("|", ">"):
            body, last = self.block_scalar(no, indent)
            return Scalar("", no, body), self.skip_to_line(i + 1, last)
        if rest[:1] in ("&", "*", "!") and not rest.startswith("!="):
            raise ScanError(no, "YAML anchors, aliases and tags are not supported")
        if rest:
            node = Scalar(rest, no)
            i += 1
            # plain multi-line continuation lines
            while i < len(self.lines) and self.lines[i][1] > indent and \
                    not KEY_RX.match(self.lines[i][2]) and not self.lines[i][2].startswith("- "):
                node.text += " " + self.lines[i][2]
                node.block = []
                i += 1
            return node, i
        i += 1
        if i < len(self.lines):
            nind, ncontent = self.lines[i][1], self.lines[i][2]
            if nind > indent:
                return self.block(i, nind)
            if nind == indent and (ncontent == "-" or ncontent.startswith("- ")):
                return self.seq(i, nind)
        return Scalar("", no), i

    def mapping(self, i, indent):
        node = Map(self.lines[i][0])
        while i < len(self.lines):
            no, ind, content = self.lines[i]
            if ind < indent:
                break
            if ind > indent:
                raise ScanError(no, "unexpected indentation")
            if content == "-" or content.startswith("- "):
                break
            m = KEY_RX.match(content)
            if not m:
                raise ScanError(no, "expected 'key: value'")
            key = unquote(m.group(1))
            value, i = self.value_after_key(i, indent, m.group(2), no)
            node.items.append((key, no, value))
        return node, i

    def seq(self, i, indent):
        node = Seq(self.lines[i][0])
        while i < len(self.lines):
            no, ind, content = self.lines[i]
            if ind != indent or not (content == "-" or content.startswith("- ")):
                if ind > indent:
                    raise ScanError(no, "unexpected indentation")
                break
            rest = content[1:].lstrip(" ")
            if not rest or rest.startswith("#"):
                i += 1
                if i < len(self.lines) and self.lines[i][1] > indent:
                    item, i = self.block(i, self.lines[i][1])
                else:
                    item = Scalar("", no)
                node.items.append(item)
                continue
            # "- key: value" starts a mapping at the column after "- "
            col = indent + (len(content) - len(rest))
            if KEY_RX.match(rest):
                self.lines[i] = (no, col, rest)
                item, i = self.mapping(i, col)
            else:
                item, i = Scalar(strip_comment(rest), no), i + 1
            node.items.append(item)
        return node, i


def walk_text(node):
    """Yield (lineno, text) for every key and scalar under node."""
    if isinstance(node, Scalar):
        for pair in node.lines():
            yield pair
    elif isinstance(node, Map):
        for k, ln, v in node.items:
            yield ln, k
            for pair in walk_text(v):
                yield pair
    elif isinstance(node, Seq):
        for item in node.items:
            for pair in walk_text(item):
                yield pair


def scalar_text(node):
    return node.value() if isinstance(node, Scalar) else ""


def expressions(text):
    """Bodies of ${{ ... }} in one line; an unterminated ${{ runs to the end."""
    out = [m.group(1) for m in EXPR_RX.finditer(text)]
    tail = EXPR_RX.sub("", text)
    if "${{" in tail:
        out.append(tail.split("${{", 1)[1])
    return out


def dynamic_secrets(text):
    """True if an expression in text reads the secrets context by a
    non-literal name (toJSON(secrets), secrets[expr], secrets.*)."""
    return any(DYNAMIC_SECRETS_RX.search(e) for e in expressions(text))


def is_model_secret(name):
    n = name.upper()
    return n in MODEL_SECRETS or bool(MODEL_SECRET_RX.match(n))


def is_local_uses(node):
    return scalar_text(node).strip().startswith(("./", "../"))


def write_scopes(job, top_perm, top_line, job_line):
    """[(scope, line)] of write permissions the job's GITHUB_TOKEN holds: the
    job's own permissions block, else the workflow's top-level one. With
    neither, the repository default applies (missing-permissions reports it)
    and nothing is returned. A flow mapping ({contents: write}) is read as text."""
    perm, line = job.get("permissions"), job.keyline("permissions")
    if perm is None:
        perm, line = top_perm, top_line
    line = line or job_line
    if perm is None:
        return []
    if isinstance(perm, Map):
        return [(k, ln) for k, ln, v in perm.items
                if isinstance(v, Scalar) and v.value().strip().lower() == "write"]
    if isinstance(perm, Scalar):
        text = perm.value().strip()
        if text == "write-all":
            return [("write-all", line)]
        return [(m.group(1), line) for m in
                re.finditer(r"([A-Za-z_-]+)\s*:\s*['\"]?write\b", text)]
    return [("unparsed-permissions", line)]  # fail closed


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

class Linter(object):
    def __init__(self, root, strict):
        self.root = root
        self.strict = strict
        self.findings = []
        self.agent_workflows = 0
        self.workflow_env = None

    def add(self, path, line, rule, msg):
        self.findings.append((path, line, rule, msg))

    def step_vendor(self, step):
        uses = scalar_text(step.get("uses")).strip()
        for vendor, spec in VENDORS.items():
            for action in spec["actions"]:
                if uses.lower().startswith(action + "@") or uses.lower() == action:
                    return vendor, "action"
        run = step.get("run")
        if isinstance(run, Scalar):
            for _, text in run.lines():
                for vendor, rx in CLI_RX.items():
                    if rx.search(text.strip()):
                        return vendor, "cli"
        return None, None

    def lint_file(self, path, text):
        rel = os.path.relpath(path, self.root)
        for no, line in enumerate(text.splitlines(), 1):
            if PLACEHOLDER in line:
                self.add(rel, no, "pin-placeholder",
                         "%s left in place: pin this action to a full commit SHA" % PLACEHOLDER)
        try:
            doc = Scanner(text).parse()
        except ScanError as exc:
            self.add(rel, exc.line, "parse-error", "cannot scan this YAML (%s); rewrite in block style" % exc)
            return
        except (IndexError, ValueError, AttributeError, TypeError) as exc:
            self.add(rel, 1, "parse-error", "scanner error (%r); fails closed" % exc)
            return
        if not isinstance(doc, Map):
            self.add(rel, 1, "parse-error", "workflow root is not a mapping")
            return
        on = doc.get("on") or doc.get("true")
        triggers = " ".join(t for _, t in walk_text(on)) if on is not None else ""
        if on is not None and isinstance(on, Scalar):
            triggers = on.value()
        privileged = bool(re.search(r"\b(pull_request_target|workflow_run)\b", triggers))

        jobs = doc.get("jobs")
        if not isinstance(jobs, Map):
            self.add(rel, doc.line, "parse-error", "no 'jobs:' mapping found")
            return

        job_info = []
        is_agent_wf = False
        for job_id, job_line, job in jobs.items:
            if not isinstance(job, Map):
                self.add(rel, job_line, "parse-error", "job %r is not a mapping" % job_id)
                continue
            steps = job.get("steps")
            step_list = [s for s in steps.items if isinstance(s, Map)] if isinstance(steps, Seq) else []
            vendors = []
            for s in step_list:
                v, kind = self.step_vendor(s)
                if v:
                    vendors.append((s, v, kind))
            if vendors:
                is_agent_wf = True
            job_info.append((job_id, job_line, job, step_list, vendors))

        # ---- rules for every workflow ----
        top_perm = doc.get("permissions")
        top_line = doc.keyline("permissions")
        self.workflow_env = doc.get("env")
        for job_id, job_line, job, steps, _ in job_info:
            self.rule_model_secret_in_write_job(rel, job_id, job_line, job, top_perm, top_line)
            for s in steps:
                self.rule_event_in_run(rel, s)
                if privileged:
                    self.rule_pwn_request(rel, s)

        agent_rules = is_agent_wf or self.strict
        if is_agent_wf:
            self.agent_workflows += 1
        if not agent_rules:
            return

        # ---- rules for agent workflows ----
        self.rule_write_all(rel, top_perm, top_line)
        for job_id, job_line, job, steps, vendors in job_info:
            perm = job.get("permissions")
            self.rule_write_all(rel, perm, job.keyline("permissions"))
            if top_perm is None and perm is None:
                self.add(rel, job_line, "missing-permissions",
                         "job %r has no permissions block and the workflow has none: declare least privilege" % job_id)
            uses = job.get("uses")
            if isinstance(uses, Scalar):
                self.rule_unpinned(rel, uses)
            for s in steps:
                u = s.get("uses")
                if isinstance(u, Scalar):
                    self.rule_unpinned(rel, u)
            if not vendors:
                continue  # job-level agent rules need an agent step
            if job.get("timeout-minutes") is None:
                self.add(rel, job_line, "missing-timeout",
                         "agent job %r has no timeout-minutes" % job_id)
            self.rule_kill_switch(rel, job_id, job_line, job, steps)
            self.rule_agent_job_write_scope(rel, job_id, job_line, job, top_perm, top_line)
            self.rule_token_to_agent(rel, job, vendors)
            self.rule_extra_secret_in_agent_job(rel, job_id, job)
            for s, vendor, kind in vendors:
                self.rule_event_in_agent_input(rel, s, kind)
                check = VENDOR_RULES.get(vendor)
                if check:
                    check(self, rel, s, kind)

    # generic rules ------------------------------------------------------

    def rule_event_in_run(self, rel, step):
        targets = []
        run = step.get("run")
        if isinstance(run, Scalar):
            targets.append(("run", run))
        uses = scalar_text(step.get("uses")).lower()
        w = step.get("with")
        if uses.startswith("actions/github-script") and isinstance(w, Map):
            sc = w.get("script")
            if isinstance(sc, Scalar):
                targets.append(("script", sc))
        for where, node in targets:
            for no, text in node.lines():
                for m in EXPR_RX.finditer(text):
                    if UNTRUSTED_RX.search(m.group(1)):
                        self.add(rel, no, "event-in-run",
                                 "expression %r interpolated into %s: pass it through env: and quote the variable"
                                 % (m.group(1).strip(), where))

    def rule_pwn_request(self, rel, step):
        uses = scalar_text(step.get("uses")).lower()
        w = step.get("with")
        if uses.startswith("actions/checkout") and isinstance(w, Map):
            ref = scalar_text(w.get("ref"))
            path = scalar_text(w.get("path")).strip()
            if PR_HEAD_RX.search(ref) and path in ("", ".", "./"):
                self.add(rel, step.line, "pwn-request-checkout",
                         "privileged trigger checks out the PR head into the workspace root; use a subdirectory and never run its scripts")
        run = step.get("run")
        if isinstance(run, Scalar):
            for no, text in run.lines():
                if re.search(r"\bgh\s+pr\s+checkout\b|\bgit\s+fetch\b.*\bpull/", text):
                    self.add(rel, no, "pwn-request-checkout",
                             "privileged trigger fetches PR code in a run step")

    def rule_write_all(self, rel, perm, line):
        if isinstance(perm, Scalar) and perm.value().strip() == "write-all":
            self.add(rel, line or perm.line, "write-all", "permissions: write-all; grant only what each job needs")

    def rule_agent_job_write_scope(self, rel, job_id, job_line, job, top_perm, top_line):
        for scope, ln in write_scopes(job, top_perm, top_line, job_line):
            if scope in WRITE_OK_FOR_AGENT:
                continue
            self.add(rel, ln, "agent-job-write-scope",
                     "agent job %r holds %s: write; the agent lane is read-only, move writes "
                     "to a separate job with no model key" % (job_id, scope))

    def rule_model_secret_in_write_job(self, rel, job_id, job_line, job, top_perm, top_line):
        scopes = [(s, ln) for s, ln in write_scopes(job, top_perm, top_line, job_line)
                  if s not in WRITE_OK_FOR_AGENT]  # id-token: write is OIDC model auth
        if job.get("permissions") is None and top_perm is None:
            # No block anywhere: the repository default token may be read/write.
            scopes = [("default-token", job_line)]
        if not scopes:
            return
        held = ", ".join(sorted(set(s for s, _ in scopes)))
        sec = job.get("secrets")
        if isinstance(sec, Scalar) and sec.value().strip() == "inherit":
            self.add(rel, sec.line, "model-secret-in-write-job",
                     "job %r holds write (%s) and passes secrets: inherit, which includes any "
                     "model key; pass only the secrets it needs" % (job_id, held))
        sources = list(walk_text(job))
        if self.workflow_env is not None:  # workflow-level env reaches every job
            sources += list(walk_text(self.workflow_env))
        for no, text in sources:
            for m in SECRET_REF_RX.finditer(text):
                name = m.group(1) or m.group(2)
                if is_model_secret(name):
                    self.add(rel, no, "model-secret-in-write-job",
                             "job %r holds write (%s) and references secrets.%s: a model "
                             "credential belongs only in a read-only agent job" % (job_id, held, name))
            if dynamic_secrets(text):
                self.add(rel, no, "model-secret-in-write-job",
                         "job %r holds write (%s) and reads the secrets context by a non-literal "
                         "name (toJSON(secrets), secrets[expr], secrets.*), which can expose a model "
                         "key; reference each secret by its literal name" % (job_id, held))
        self.rule_secret_to_local_call(rel, job_id, job, held)

    def rule_secret_to_local_call(self, rel, job_id, job, held):
        """A write job that hands secrets to a local reusable workflow (job
        'uses: ./...') or composite action (step 'uses: ./...'): the linter
        does not follow the call, so it cannot see whether a model key goes
        there. secrets: inherit is reported by model-secret-in-write-job."""
        msg = ("job %r holds write (%s) and passes %s to local %s %s, which this linter "
               "does not follow; keep the call in a job with no write scope or inline it")
        if is_local_uses(job.get("uses")):
            target = scalar_text(job.get("uses")).strip()
            sec = job.get("secrets")
            if isinstance(sec, Map):
                for key, ln, _ in sec.items:
                    self.add(rel, ln, "secret-to-local-call",
                             msg % (job_id, held, "secret %r" % key, "reusable workflow", target))
            elif isinstance(sec, Scalar) and sec.value().strip() not in ("", "inherit"):
                self.add(rel, sec.line, "secret-to-local-call",
                         msg % (job_id, held, "secrets", "reusable workflow", target))
        steps = job.get("steps")
        for s in (steps.items if isinstance(steps, Seq) else []):
            if not isinstance(s, Map) or not is_local_uses(s.get("uses")):
                continue
            target = scalar_text(s.get("uses")).strip()
            for key in ("with", "env"):
                for no, text in walk_text(s.get(key)) if s.get(key) is not None else []:
                    names = [m.group(1) or m.group(2) for m in SECRET_REF_RX.finditer(text)]
                    names = [n for n in names if n != "GITHUB_TOKEN"]
                    if dynamic_secrets(text):
                        names.append("a non-literal secrets reference")
                    for n in names:
                        self.add(rel, no, "secret-to-local-call",
                                 msg % (job_id, held, n if " " in n else "secrets." + n,
                                        "composite action", target))

    def rule_extra_secret_in_agent_job(self, rel, job_id, job):
        """An agent job receives only GITHUB_TOKEN and model credentials."""
        sources = list(walk_text(job))
        if self.workflow_env is not None:
            sources += list(walk_text(self.workflow_env))
        seen = set((p, ln) for p, ln, r, _ in self.findings if r == "token-to-agent")
        for no, text in sources:
            if (rel, no) in seen:
                continue
            for m in SECRET_REF_RX.finditer(text):
                name = m.group(1) or m.group(2)
                if name != "GITHUB_TOKEN" and not is_model_secret(name):
                    self.add(rel, no, "extra-secret-in-agent-job",
                             "agent job %r receives secrets.%s: the agent lane gets only "
                             "GITHUB_TOKEN and a model credential; move this to a job without "
                             "the agent" % (job_id, name))
            if dynamic_secrets(text):
                self.add(rel, no, "extra-secret-in-agent-job",
                         "agent job %r reads the secrets context by a non-literal name "
                         "(toJSON(secrets), secrets[expr], secrets.*), which exposes every "
                         "secret; reference each secret by its literal name" % job_id)

    def rule_unpinned(self, rel, uses):
        ref = uses.value().strip()
        if ref.startswith("./") or ref.startswith("../"):
            return
        if ref.startswith("docker://"):
            if "@sha256:" not in ref:
                self.add(rel, uses.line, "unpinned-uses", "%s: pin the image by @sha256 digest" % ref)
            return
        if "@" not in ref:
            self.add(rel, uses.line, "unpinned-uses", "%s has no ref: pin to a full 40-char commit SHA" % ref)
            return
        pin = ref.rsplit("@", 1)[1]
        if not SHA_RX.match(pin):
            self.add(rel, uses.line, "unpinned-uses",
                     "%s is not pinned to a full 40-char commit SHA" % ref)

    def rule_kill_switch(self, rel, job_id, job_line, job, steps):
        cond = scalar_text(job.get("if"))
        if KILL_SWITCH in cond:
            return
        if steps:
            first = steps[0]
            text = scalar_text(first.get("if")) + " " + scalar_text(first.get("run"))
            if KILL_SWITCH in text:
                return
        self.add(rel, job_line, "missing-kill-switch",
                 "agent job %r does not check %s in its job 'if:' or first step" % (job_id, KILL_SWITCH))

    def rule_token_to_agent(self, rel, job, vendors):
        def check_map(m):
            if not isinstance(m, Map):
                return
            for key, ln, node in m.items:
                if not isinstance(node, Scalar):
                    continue
                k = key.lower().replace("-", "_")
                if k in ("github_token", "gh_token", "gh_enterprise_token"):
                    for m2 in re.finditer(r"secrets\.([A-Za-z0-9_]+)", node.value()):
                        if m2.group(1) != "GITHUB_TOKEN":
                            self.add(rel, ln, "token-to-agent",
                                     "%s gets secrets.%s (a PAT or app token): agent jobs get only the job-scoped GITHUB_TOKEN"
                                     % (key, m2.group(1)))
        check_map(job.get("env"))
        for s, _, _ in vendors:
            check_map(s.get("env"))
            check_map(s.get("with"))

    def rule_event_in_agent_input(self, rel, step, kind):
        w = step.get("with") if kind == "action" else None
        if isinstance(w, Map):
            for no, text in walk_text(w):
                for m in EXPR_RX.finditer(text):
                    if UNTRUSTED_RX.search(m.group(1)):
                        self.add(rel, no, "event-in-agent-input",
                                 "expression %r interpolated into the agent's input: untrusted text is data, let the agent read it through a tool"
                                 % m.group(1).strip())


# vendor rules -------------------------------------------------------------

def claude_rules(lint, rel, step, kind):
    """anthropics/claude-code-action inputs and claude CLI flags, as named in
    the action's action.yml and the CLI reference."""
    if kind == "action":
        w = step.get("with")
        w = w if isinstance(w, Map) else Map(step.line)
        args_node = w.get("claude_args")
        args = scalar_text(args_node)
        args_line = args_node.line if isinstance(args_node, Scalar) else step.line
        for key in ("allowed_non_write_users", "allowed_bots"):
            v = w.get(key)
            if isinstance(v, Scalar) and v.value().strip() == "*":
                lint.add(rel, v.line, "wildcard-actor",
                         "%s: \"*\" lets anyone drive the agent; list explicit names" % key)
        v = w.get("show_full_output")
        if isinstance(v, Scalar) and v.value().strip().lower() == "true":
            lint.add(rel, v.line, "show-full-output",
                     "show_full_output: true leaks tool output to public logs; keep it off on public repos")
        settings = scalar_text(w.get("settings"))
    else:
        run = step.get("run")
        args = scalar_text(run)
        args_line = run.line if isinstance(run, Scalar) else step.line
        settings = ""
    if not re.search(r"--allowed-?[Tt]ools\b", args):
        lint.add(rel, args_line, "missing-allowed-tools",
                 "agent runs without an --allowedTools allowlist")
    entries = allowed_tool_entries(args)
    if kind == "action":
        legacy = w.get("allowed_tools")  # pre-v1 action input
        if isinstance(legacy, Scalar):
            entries += split_tool_list(legacy.value())
    for m in re.finditer(r'"allow"\s*:\s*\[([^\]]*)\]', settings):
        entries += re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    for entry in entries:
        if unrestricted_shell(entry):
            lint.add(rel, args_line, "unrestricted-bash",
                     "allowed tools include %r, which matches every shell command; "
                     "list the exact commands, e.g. Bash(npm test)" % entry)
            continue
        why = risky_shell_wildcard(entry)
        if why:
            lint.add(rel, args_line, "unrestricted-bash",
                     "allowed tools include %r: %s; list fixed commands, e.g. Bash(git diff --stat)"
                     % (entry, why))
    # Permission bypass: flags from the CLI reference, defaultMode from the
    # settings schema. Either one turns the allowlist into a no-op.
    bypass_text = args + "\n" + settings
    m = re.search(r"--dangerously-skip-permissions\b|--allow-dangerously-skip-permissions\b|"
                  r"--permission-mode(?:\s+|\s*=\s*)['\"]?bypassPermissions\b", args)
    if m:
        lint.add(rel, args_line, "permission-bypass",
                 "%s skips every permission check, so the allowlist does not apply" % m.group(0))
    if re.search(r"\\?[\"']defaultMode\\?[\"']\s*:\s*\\?[\"']bypassPermissions", bypass_text):
        line = args_line
        if kind == "action" and isinstance(w.get("settings"), Scalar):
            line = w.get("settings").line
        lint.add(rel, line, "permission-bypass",
                 "settings set \"defaultMode\": \"bypassPermissions\", which skips every "
                 "permission check, so the allowlist does not apply")


# Per the Claude Code permissions docs, a bare "Bash" and "Bash(*)" both match
# every command, and ":*" is a trailing-wildcard spelling; PowerShell rules use
# the same shape.
_SHELL_RULE_RX = re.compile(r"^(Bash|PowerShell)\s*(?:\(\s*([^)]*?)\s*\))?$", re.I)


def unrestricted_shell(entry):
    m = _SHELL_RULE_RX.match(entry.strip())
    if not m:
        return False
    inner = m.group(2)
    return inner is None or re.sub(r"[\s:*]", "", inner) == ""


# Programs that run another command, script or expression from their
# arguments: with a "*" after them the rule allows any command. Includes the
# wrappers Claude Code strips before matching (timeout, nohup, xargs, ...),
# exec wrappers it does not strip (watch, setsid, flock), and env runners.
_EXEC_PROGRAMS = set("""
    bash sh zsh dash ksh mksh ash fish csh tcsh busybox pwsh powershell cmd
    python python2 python3 pypy pypy3 node nodejs deno bun ruby perl php lua
    luajit tclsh rscript osascript awk gawk mawk nawk sed find
    env xargs sudo doas su eval exec command builtin nohup timeout time nice
    stdbuf watch setsid flock ionice nocorrect noglob chroot unshare nsenter
    script strace ltrace gdb npx bunx pnpx uvx
""".split())
_EXEC_RX = re.compile(r"^(?:python|pypy|ruby|perl|php|node|lua)[\d.]*$")
_VCS_PROGRAMS = {"git", "gh", "glab", "hub"}
# "<runner> <sub> *" forms that run an arbitrary command or package binary.
# Project scripts (npm test *, npm run *, pytest *) stay allowed.
_RUNNER_SUBS = {"npm": {"exec", "x"}, "pnpm": {"exec", "dlx"}, "yarn": {"exec", "dlx"},
                "bun": {"x", "exec"}, "uv": {"run", "tool"}, "pipx": {"run"},
                "poetry": {"run"}, "pdm": {"run"}, "hatch": {"run"}, "rye": {"run"},
                "conda": {"run"}, "mamba": {"run"}, "pixi": {"run", "exec"},
                "docker": {"run", "exec"}, "podman": {"run", "exec"},
                "kubectl": {"exec", "run"}, "direnv": {"exec"}, "devbox": {"run"},
                "mise": {"exec", "x"}, "nix": {"run", "shell", "develop"}}


def risky_shell_wildcard(entry):
    """Why a Bash/PowerShell rule with a wildcard is effectively unrestricted,
    or None. Claude Code matches everything before the first "*" literally, and
    ":*" at the end is a trailing " *"."""
    m = _SHELL_RULE_RX.match(entry.strip())
    if not m or m.group(2) is None or "*" not in m.group(2):
        return None
    inner = re.sub(r":\*$", " *", m.group(2).strip())
    words = inner.split("*", 1)[0].split()
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words.pop(0)  # FOO=1 cmd *
    if not words:
        return "a wildcard before the program name matches any command"
    prog = words[0].rsplit("/", 1)[-1].lower()
    if prog in _EXEC_PROGRAMS or _EXEC_RX.match(prog):
        return "%r runs whatever command or code follows it" % prog
    if prog in _VCS_PROGRAMS:
        return ("a wildcard on %r reaches subcommands and flags that read and write arbitrary "
                "files or the remote (git diff --no-index/--output, -c, push, gh api)"
                % " ".join(words))
    if len(words) >= 2 and words[1].lower() in _RUNNER_SUBS.get(prog, ()):
        return "%r runs an arbitrary command or package binary" % " ".join(words[:2])
    if len(words) == 1 and prog in _RUNNER_SUBS:
        return "%r with a wildcard reaches %s" % (prog, "/".join(sorted(_RUNNER_SUBS[prog])))
    return None


def split_tool_list(text):
    """Split "Read,Bash(git diff *) Grep" on commas and whitespace outside
    parentheses."""
    out, cur, depth = [], [], 0
    for c in text:
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if depth == 0 and (c == "," or c.isspace()):
            if cur:
                out.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    if cur:
        out.append("".join(cur))
    return [unquote(t) for t in out if unquote(t)]


def allowed_tool_entries(args):
    """Entries given to --allowedTools / --allowed-tools in a CLI argument
    string: quoted values, or bare words up to the next "--" flag."""
    out = []
    for m in re.finditer(r"--allowed-?[Tt]ools(?:=|\s+)", args):
        rest = args[m.end():]
        q = re.match(r"""\s*(["'])(.*?)\1""", rest, re.S)
        if q:
            out += split_tool_list(q.group(2))
            continue
        words = []
        for word in re.split(r"\s+", rest.strip()):
            if word.startswith("-") or not word:
                break
            if word != "\\":  # shell line continuation
                words.append(word)
        out += split_tool_list(" ".join(words))
    return out


VENDOR_RULES = {
    "claude": claude_rules,
    # codex, gemini, cursor, opencode: generic agent rules only until their
    # allowlist and bypass flags are confirmed from their own docs.
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lint agent workflows.")
    ap.add_argument("--root", default=".", help="repository root (default: .)")
    ap.add_argument("--strict", action="store_true",
                    help="apply agent-workflow rules to every workflow")
    ap.add_argument("files", nargs="*", help="workflow files (default: .github/workflows/*.y*ml)")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    files = args.files
    if not files:
        wf = os.path.join(root, ".github", "workflows")
        if os.path.isdir(wf):
            files = sorted(os.path.join(wf, f) for f in os.listdir(wf)
                           if f.endswith((".yml", ".yaml")))
    lint = Linter(root, args.strict)
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            lint.add(os.path.relpath(f, root), 1, "parse-error", "cannot read file (%s)" % exc)
            continue
        lint.lint_file(os.path.abspath(f), text)
    for path, line, rule, msg in lint.findings:
        print("%s:%d %s %s" % (path, line, rule, msg))
    status = "FAIL" if lint.findings else "PASS"
    print("HARNESS_WORKFLOW_LINT: %s (%d findings, %d workflows, %d agent workflows)"
          % (status, len(lint.findings), len(files), lint.agent_workflows))
    return 1 if lint.findings else 0


if __name__ == "__main__":
    sys.exit(main())
