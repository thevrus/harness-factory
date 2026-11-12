"""GitHub Actions workflow helpers (text level; no YAML library in stdlib)."""
from __future__ import annotations

import re
from typing import Dict, List, NamedTuple

# Markers that a workflow invokes a coding agent. Comment lines are stripped
# first so a mention in a comment does not count.
AGENT_PATTERNS = [
    re.compile(r"anthropics/claude-code(?:-base)?-action"),
    re.compile(r"@anthropic-ai/claude-code"),
    re.compile(r"(?m)(?:^|[\s;&|(`])claude\s+(?:-p\b|--print\b|-c\b|--continue\b|--resume\b|"
               r"--output-format\b|--max-turns\b|--permission-mode\b|--model\b|--allowedTools\b|"
               r"--bare\b|[\"'])"),
]
_JOBS_RE = re.compile(r"^jobs:\s*$")
_JOB_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_\-]+):\s*$")
_IF_RE = re.compile(r"^\s*if:(.*)$")


class Workflow(NamedTuple):
    path: str
    text: str   # comment lines removed
    raw: str


def strip_comments(text: str) -> str:
    return "\n".join("" if l.lstrip().startswith("#") else l for l in text.splitlines())


def _ctx_cache(ctx) -> dict:
    """Per-RepoContext memo for the helpers below (created on first use)."""
    cache = getattr(ctx, "_workflow_cache", None)
    if cache is None:
        cache = ctx._workflow_cache = {}
    return cache


def load_workflows(ctx) -> List[Workflow]:
    """Workflows under .github/workflows (memoized per ctx; a fresh list each call)."""
    cache = _ctx_cache(ctx)
    if "all" not in cache:
        cache["all"] = _load_workflows(ctx)
    return list(cache["all"])


def _load_workflows(ctx) -> List[Workflow]:
    out = []
    for name in ctx.listdir(".github/workflows"):
        if name.endswith((".yml", ".yaml")):
            rel = ".github/workflows/" + name
            raw = ctx.read_text(rel)
            if raw is not None:
                out.append(Workflow(rel, strip_comments(raw), raw))
    return out


def is_agent_text(text: str) -> bool:
    return any(p.search(text) for p in AGENT_PATTERNS)


def agent_workflows(ctx) -> List[Workflow]:
    cache = _ctx_cache(ctx)
    if "agent" not in cache:
        cache["agent"] = [w for w in load_workflows(ctx) if is_agent_text(w.text)]
    return list(cache["agent"])


_SPLIT_CACHE: Dict[str, Dict[str, str]] = {}


def split_jobs(text: str) -> Dict[str, str]:
    """Split a workflow into {job_id: job_text} by indentation (memoized by text; a fresh dict each call)."""
    if text not in _SPLIT_CACHE:
        _SPLIT_CACHE[text] = _split_jobs(text)
    return dict(_SPLIT_CACHE[text])


def _split_jobs(text: str) -> Dict[str, str]:
    lines = text.splitlines()
    start = None
    for i, l in enumerate(lines):
        if _JOBS_RE.match(l):
            start = i + 1
            break
    if start is None:
        return {}
    jobs: Dict[str, List[str]] = {}
    indent = None
    current = None
    for l in lines[start:]:
        if not l.strip():
            if current:
                jobs[current].append(l)
            continue
        ind = len(l) - len(l.lstrip(" "))
        if ind == 0:
            break  # next top-level key
        if indent is None:
            indent = ind
        m = _JOB_KEY_RE.match(l)
        if ind == indent and m:
            current = m.group(1)
            jobs[current] = []
        elif current:
            jobs[current].append(l)
    return {k: "\n".join(v) for k, v in jobs.items()}


def job_if(body: str) -> str:
    """The job-level `if:` expression of a job body from split_jobs ('' if none).

    Only the key at the job's own indentation counts, so a step-level `if:`
    or an `if` inside a run script is not mistaken for the job condition.
    Folded/literal values (`if: >-` + indented lines) are joined.
    """
    lines = body.splitlines()
    indents = [len(l) - len(l.lstrip(" ")) for l in lines if l.strip()]
    if not indents:
        return ""
    key_ind = min(indents)
    for i, l in enumerate(lines):
        ind = len(l) - len(l.lstrip(" "))
        m = _IF_RE.match(l)
        if ind != key_ind or not m:
            continue
        parts = [m.group(1)]
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= key_ind:
                break
            parts.append(nxt)
        return " ".join(p.strip() for p in parts).strip()
    return ""


def references(text: str, needle: str) -> bool:
    return needle in text


_NEUTERED_TAIL = re.compile(r"(\|\|\s*(true|:|exit\s+0)|;\s*true)\s*[\"']?\s*$")


def triggers_on_pull_request(text: str) -> bool:
    """True when the workflow's `on:` names pull_request (not pull_request_target)."""
    m = re.search(r"(?m)^(?:on|\"on\"|'on'):(.*)$", text)
    if not m:
        return False
    block = [m.group(1)]
    for l in text[m.end():].splitlines()[1:]:
        if l.strip() and not l.startswith((" ", "\t")):
            break
        block.append(l)
    return re.search(r"(?<![A-Za-z_])pull_request(?![A-Za-z_])", "\n".join(block)) is not None


def neutered_reason(text: str, needles) -> str:
    """Why a workflow that mentions the needles does not really enforce them ('' if it does)."""
    for body_name, body in (split_jobs(text) or {"": text}).items():
        if not any(n in body for n in needles):
            continue
        where = "job '%s'" % body_name if body_name else "workflow"
        if re.match(r"^(false|\$\{\{\s*false\s*\}\})$", job_if(body).strip("'\" ")):
            return "%s is disabled with if: false" % where
        if re.search(r"(?m)^\s*continue-on-error:\s*true\s*$", body):
            return "%s sets continue-on-error: true" % where
        for l in body.splitlines():
            if any(n in l for n in needles) and _NEUTERED_TAIL.search(l):
                return "%s ignores the result (`%s`)" % (where, l.strip()[:80])
    return ""
