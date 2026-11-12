#!/usr/bin/env python3
"""harness-factory installer: merge-aware, idempotent, verifiable.

Usage:
  hf_install.py plan   --repo PATH --rung N [--json]
  hf_install.py apply  --repo PATH --rung N [--on-conflict stop|sidecar|replace] [--json]
  hf_install.py verify --repo PATH --rung N [--json]
  hf_install.py config init --repo PATH [--from-audit AUDIT.json]
  hf_install.py config set  --repo PATH KEY VALUE
  hf_install.py config show --repo PATH
  hf_install.py sync   --repo PATH

config set takes a dotted KEY; VALUE is parsed as JSON, else kept as a string.
sync rewrites the "# harness:config:start/end" block in
.harness/protected-paths.txt (protected_paths + human_only_areas) and
.harness/maintainers.txt (maintainers) when those files exist; text outside
the block is untouched. Once the agent's edit tool is gated on .harness/**,
this command is how config lists reach those files, and running it is the
human approval. apply runs sync at the end; verify checks the blocks.

Exit codes: 0 ok; 1 conflicts (plan/apply) or misses (verify); 2 usage or
tool error (bad config, unresolved variable, invalid JSON, path escape).

Why this exists: copy-if-absent installers silently skip CLAUDE.md and
.claude/settings.json in repos that already have them, so the safety layer
never lands. This installer merges into existing files and `verify` checks
what is in effect afterwards.

Manifest (templates/rungN/manifest.json):
  {"rung": N, "name": "...", "requires_config": ["commands.test"],
   "files": [{"src", "dest", "strategy", "mode"?, "marker"?, "when"?,
              "unless"?, "once"?, "dest_any"?, "aliases"?, "skip_if_same_file"?}]}

Strategies:
  create       write if absent; identical is a no-op; different is a CONFLICT,
               unless installed.json shows the file is ours and unmodified, in
               which case it is updated in place.
  json-merge   deep merge: objects merge, arrays union without duplicates,
               hook handlers de-duplicated by `command`; existing keys are never
               deleted or reordered. A differing scalar is a CONFLICT.
  md-section   managed block between <!-- harness:MARKER:start --> and
               <!-- harness:MARKER:end -->; replaced if present, else appended.
               With "once": true an existing block is never rewritten (for
               sections a human fills in).
  line-append  add only the missing lines.

Entry options:
  agent               adapter id (e.g. "claude"). Entries without it are core
                      and always install; an adapter entry installs only when
                      its id is in config "agents" (default ["claude"]).
                      A list (e.g. ["codex", "cursor"]) installs when any
                      of its ids is configured; several entries may then
                      target the same dest, so give each list entry a
                      dest no single-agent entry also writes.
  when / unless       skip the entry if the config value is empty / non-empty.
  dest_any            alternative existing paths to use instead of dest
                      (first that exists wins), e.g. an uppercase PR template.
  aliases             line-append only: {"line": ["equivalent", ...]}; a line
                      counts as present if an alias is present.
  skip_if_same_file   skip when dest and this path are the same file
                      (e.g. AGENTS.md symlinked to CLAUDE.md).

Template syntax (rendered from <repo>/.harness/config.json):
  {{dotted.key}}         value; aborts if the key is absent from config.
  {{dotted.key|code}}    markdown inline code, pipes escaped (table-safe).
  {{dotted.key|sh}}      POSIX single-quoted shell word.
  {{dotted.key|json}}    JSON encoding (for lists and objects).
  Line guards at the start of a line:
  {{?dotted.key}}...     keep the line only if the value is non-empty.
  {{!dotted.key}}...     keep the line only if the value is empty.
"""

import argparse
import copy
import datetime
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hf_checks.installed import (  # noqa: E402
    GUARDED_DIRS, STATE_FILE, SYNC_END, SYNC_RE, SYNC_START, content_sha, guard_problem, sha256,
    strip_config_block)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
LEDGER_FILE = ".harness/ledger.jsonl"
CONFIG_FILE = ".harness/config.json"
STRATEGIES = ("create", "json-merge", "md-section", "line-append")
CLAUDE_SETTINGS = ".claude/settings.json"
CLAUDE_LOCAL_SETTINGS = ".claude/settings.local.json"

# The lookbehind keeps GitHub Actions expressions (`${{ github.x }}`) out of
# template substitution; workflow templates are full of them.
VAR_RE = re.compile(r"(?<!\$)\{\{\s*([A-Za-z0-9_.]+)\s*(?:\|\s*([a-z]+)\s*)?\}\}")
GUARD_RE = re.compile(r"^\{\{([?!])([A-Za-z0-9_.]+)\}\}")


class HFError(Exception):
    """Fatal error: abort before any write, exit 2."""


def hf_version():
    # One definition, shared with the audit (hf_checks/__init__.py).
    from hf_checks import HF_VERSION
    return HF_VERSION


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def newline_of(text):
    """The file's line ending: CRLF when its first line break is CRLF."""
    i = text.find("\n") if text else -1
    return "\r\n" if i > 0 and text[i - 1] == "\r" else "\n"


def to_newline(text, nl):
    return text if nl == "\n" else text.replace("\r\n", "\n").replace("\n", nl)


# ---------------------------------------------------------------- config

MISSING = object()


def lookup(cfg, key):
    cur = cfg
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def is_empty(value):
    return value is MISSING or value is None or value == "" or value == [] or value == {}


def load_config(repo):
    path = os.path.join(repo, CONFIG_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except ValueError as e:
        raise HFError("%s is not valid JSON: %s" % (CONFIG_FILE, e))
    if not isinstance(cfg, dict) or cfg.get("schema") != 1:
        raise HFError('%s must be an object with "schema": 1' % CONFIG_FILE)
    return cfg


def render(text, cfg, src):
    """Apply line guards then variable substitution. Unresolved keys abort."""
    errors = []

    def value_of(key):
        if cfg is None:
            errors.append("%s: needs %s but %s is missing" % (src, key, CONFIG_FILE))
            return ""
        v = lookup(cfg, key)
        if v is MISSING:
            errors.append("%s: unresolved variable {{%s}}" % (src, key))
            return ""
        return v

    out_lines = []
    for line in text.splitlines(True):
        m = GUARD_RE.match(line)
        if m:
            v = value_of(m.group(2))
            keep = (not is_empty(v)) if m.group(1) == "?" else is_empty(v)
            if not keep:
                continue
            line = line[m.end():]
        out_lines.append(line)
    text = "".join(out_lines)

    def sub(m):
        key, filt = m.group(1), m.group(2)
        v = value_of(key)
        if filt == "json":
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, (dict, list)):
            errors.append("%s: {{%s}} is a %s; use |json" % (src, key, type(v).__name__))
            return ""
        s = "" if v is None else (str(v).lower() if isinstance(v, bool) else str(v))
        if filt == "sh":
            return shlex.quote(s)
        if "\n" in s:
            errors.append("%s: {{%s}} contains a newline; only |sh or |json may" % (src, key))
            return ""
        if filt == "code":
            if not s:
                return ""
            fence = "``" if "`" in s else "`"
            pad = " " if "`" in s else ""
            return "%s%s%s%s%s" % (fence, pad, s.replace("|", "\\|"), pad, fence)
        if filt:
            errors.append("%s: unknown filter |%s" % (src, filt))
        return s

    text = VAR_RE.sub(sub, text)
    if errors:
        raise HFError("\n".join(errors))
    return text


# ---------------------------------------------------------------- paths

def safe_dest(repo, rel):
    """Resolve rel inside repo; refuse escapes, including via symlinks."""
    if not rel or os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        raise HFError("refusing dest outside the repo: %r" % rel)
    real_repo = os.path.realpath(repo)
    real = os.path.realpath(os.path.join(repo, rel))
    if real != real_repo and not real.startswith(real_repo + os.sep):
        raise HFError("refusing dest that resolves outside the repo (symlink?): %s -> %s" % (rel, real))
    return real


def read_text(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raise HFError("%s is not UTF-8 text; refusing to touch it" % path)


# ---------------------------------------------------------------- json merge

def is_hook_list(path):
    # hooks.<Event> is a list of {matcher, hooks:[handler...]} groups
    return len(path) == 2 and path[0] == "hooks"


def handler_key(h):
    if isinstance(h, dict):
        return ("cmd", h.get("command"), json.dumps(h.get("args"))) if "command" in h else ("raw", json.dumps(h, sort_keys=True))
    return ("raw", json.dumps(h, sort_keys=True))


def merge_hook_groups(existing, fragment, added):
    out = copy.deepcopy(existing)
    present = set()
    for g in out:
        if isinstance(g, dict):
            # Heal installs from versions that leaked the internal _event key.
            if "_event" in g:
                g.pop("_event")
                added.append("drop internal _event key from hook group %s[%s]" % (
                    fragment[0].get("_event", "") if fragment else "", g.get("matcher", "")))
            for h in g.get("hooks", []) or []:
                present.add(handler_key(h))
    for fg in fragment:
        matcher = fg.get("matcher", "")
        for h in fg.get("hooks", []):
            if handler_key(h) in present:
                continue
            target = None
            for g in out:
                if isinstance(g, dict) and g.get("matcher", "") == matcher and isinstance(g.get("hooks"), list):
                    target = g
                    break
            if target is None:
                target = OrderedDict((k, copy.deepcopy(v)) for k, v in fg.items()
                                     if k not in ("hooks", "_event"))
                target["hooks"] = []
                out.append(target)
            target["hooks"].append(copy.deepcopy(h))
            present.add(handler_key(h))
            added.append("hook %s: %s" % (fg.get("_event", ""), h.get("command") if isinstance(h, dict) else h))
    return out


def json_merge(existing, fragment, path, added, conflicts, prefer_fragment):
    if isinstance(existing, dict) and isinstance(fragment, dict):
        out = OrderedDict(existing)
        for k, fv in fragment.items():
            p = path + [k]
            if k not in out:
                out[k] = copy.deepcopy(fv)
                added.append("key " + ".".join(p))
            else:
                out[k] = json_merge(out[k], fv, p, added, conflicts, prefer_fragment)
        return out
    if isinstance(existing, list) and isinstance(fragment, list):
        if is_hook_list(path) and all(isinstance(g, dict) for g in fragment):
            for g in fragment:
                g["_event"] = path[1]
            try:
                return merge_hook_groups(existing, fragment, added)
            finally:
                for g in fragment:
                    g.pop("_event", None)
        out = list(existing)
        for item in fragment:
            if item not in out:
                out.append(copy.deepcopy(item))
                added.append("item %s += %s" % (".".join(path), json.dumps(item)[:60]))
        return out
    if existing == fragment:
        return existing
    conflicts.append("%s: existing %s, harness wants %s" % (
        ".".join(path) or "<root>", json.dumps(existing)[:60], json.dumps(fragment)[:60]))
    return copy.deepcopy(fragment) if prefer_fragment else existing


def json_subset(frag, actual, path, misses):
    """Is every part of the fragment in effect in actual?"""
    if isinstance(frag, dict):
        if not isinstance(actual, dict):
            misses.append("%s is not an object" % ".".join(path))
            return
        for k, v in frag.items():
            if k not in actual:
                misses.append("missing key %s" % ".".join(path + [k]))
            else:
                json_subset(v, actual[k], path + [k], misses)
    elif isinstance(frag, list):
        if not isinstance(actual, list):
            misses.append("%s is not an array" % ".".join(path))
            return
        if is_hook_list(path):
            for fg in frag:
                for h in fg.get("hooks", []):
                    ok = any(isinstance(g, dict) and g.get("matcher", "") == fg.get("matcher", "")
                             and any(handler_key(x) == handler_key(h) and all(x.get(k) == v for k, v in h.items())
                                     for x in g.get("hooks", []) or [] if isinstance(x, dict))
                             for g in actual)
                    if not ok:
                        misses.append("hook %s[%s] %s not registered as shipped" % (
                            path[1], fg.get("matcher", ""), h.get("command")))
            return
        for item in frag:
            if item not in actual:
                misses.append("%s lacks %s" % (".".join(path), json.dumps(item)[:60]))
    elif frag != actual:
        misses.append("%s = %s, expected %s" % (".".join(path), json.dumps(actual)[:40], json.dumps(frag)[:40]))


def detect_indent(text):
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line):
            return len(line) - len(stripped)
    return 2


def dump_json(obj, indent):
    return json.dumps(obj, indent=indent, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- md / lines

def markers(name):
    return "<!-- harness:%s:start -->" % name, "<!-- harness:%s:end -->" % name


def find_block(text, name):
    start, end = markers(name)
    s_count, e_count = text.count(start), text.count(end)
    if s_count == 0 and e_count == 0:
        return None
    s, e = text.find(start), text.find(end)
    if s_count != 1 or e_count != 1 or e < s:
        raise HFError("malformed managed block %r (need exactly one start then one end marker)" % name)
    return s, e + len(end)


def make_block(name, body):
    start, end = markers(name)
    return "%s\n%s\n%s" % (start, body.strip("\n"), end)


def norm_lines(text):
    return [line.strip() for line in text.splitlines()]


# ---------------------------------------------------------------- planning

class Item(object):
    def __init__(self, entry, dest):
        self.entry = entry
        self.dest = dest            # repo-relative, as reported
        self.real = None            # absolute write target
        self.strategy = entry["strategy"]
        self.action = None          # create|update|merge|unchanged|skip|conflict
        self.detail = ""
        self.new_text = None        # what we will write to dest
        self.sidecar_text = None    # what we write on --on-conflict sidecar/replace
        self.mode = entry.get("mode")
        self.conflicts = []

    def as_dict(self):
        d = OrderedDict([("dest", self.dest), ("strategy", self.strategy), ("action", self.action)])
        if self.detail:
            d["detail"] = self.detail
        if self.conflicts:
            d["conflicts"] = self.conflicts
        return d


def load_manifest(rung):
    path = os.path.join(TEMPLATES, "rung%d" % rung, "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except FileNotFoundError:
        raise HFError("no manifest for rung %d (%s)" % (rung, path))
    except ValueError as e:
        raise HFError("invalid manifest %s: %s" % (path, e))
    for e in m.get("files", []):
        if e.get("strategy") not in STRATEGIES:
            raise HFError("manifest entry %s: unknown strategy %r" % (e.get("src"), e.get("strategy")))
        if e["strategy"] == "md-section" and not e.get("marker"):
            raise HFError("manifest entry %s: md-section needs a marker" % e.get("src"))
    return m


def load_state(repo):
    text = read_text(os.path.join(repo, STATE_FILE))
    if not text:
        return OrderedDict([("hf_version", None), ("files", OrderedDict())])
    try:
        st = json.loads(text, object_pairs_hook=OrderedDict)
    except ValueError as e:
        raise HFError("%s is not valid JSON: %s" % (STATE_FILE, e))
    st.setdefault("files", OrderedDict())
    return st


def check_requires(manifest, cfg):
    missing = []
    for key in manifest.get("requires_config", []):
        if cfg is None or is_empty(lookup(cfg, key)):
            missing.append(key)
    if missing:
        raise HFError("rung %s requires non-empty config values in %s: %s" % (
            manifest.get("rung"), CONFIG_FILE, ", ".join(missing)))


def configured_agents(cfg):
    agents = lookup(cfg, "agents") if cfg is not None else MISSING
    if agents is MISSING:
        return ["claude"]
    if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
        raise HFError('%s: "agents" must be an array of agent ids' % CONFIG_FILE)
    return agents


def skip_reason(entry, cfg):
    """Why an entry does not apply under this config, or None."""
    agent = entry.get("agent")
    if agent:
        wanted = [agent] if isinstance(agent, str) else list(agent)
        if not set(wanted) & set(configured_agents(cfg)):
            return "adapter for agent %s, not in config agents" % "/".join(wanted)
    for key, want_empty in (("when", False), ("unless", True)):
        k = entry.get(key)
        if k:
            v = MISSING if cfg is None else lookup(cfg, k)
            if is_empty(v) != want_empty:
                return "%s is %s" % (k, "empty" if not want_empty else "set")
    return None


def exists_exact(repo, rel):
    """lexists, but case-exact even on case-insensitive filesystems."""
    path = os.path.join(repo, rel)
    if not os.path.lexists(path):
        return False
    try:
        return os.path.basename(path) in os.listdir(os.path.dirname(path) or ".")
    except OSError:
        return False


def pick_dest(repo, entry):
    for cand in [entry["dest"]] + list(entry.get("dest_any", [])):
        if exists_exact(repo, cand):
            return cand
    return entry["dest"]


def plan_entry(repo, rung, entry, cfg, state, overlay):
    dest = pick_dest(repo, entry)
    item = Item(entry, dest)
    why = skip_reason(entry, cfg)
    if why:
        item.action, item.detail = "skip", why
        return item
    item.real = safe_dest(repo, dest)
    same = entry.get("skip_if_same_file")
    if same:
        other = safe_dest(repo, same)
        if os.path.exists(item.real) and os.path.exists(other) and os.path.samefile(item.real, other):
            item.action = "skip"
            item.detail = "%s and %s are the same file" % (dest, same)
            return item
    src_path = os.path.join(TEMPLATES, "rung%d" % rung, entry["src"])
    src_text = read_text(src_path)
    if src_text is None:
        raise HFError("template missing: %s" % src_path)
    body = render(src_text, cfg, entry["src"])
    if os.path.isdir(item.real):
        raise HFError("dest is a directory: %s" % dest)
    # overlay holds text planned by earlier entries for the same file, so two
    # entries (e.g. core and adapter .gitignore lines) compose instead of clobbering
    cur = overlay[item.real] if item.real in overlay else read_text(item.real)
    strat = item.strategy

    if strat == "create":
        # sync owns the config block; compare and update the rest of the file
        cur = strip_config_block(cur) if cur is not None else None
        if cur is None:
            item.action, item.new_text = "create", body
        elif cur == body:
            item.action = "unchanged"
        else:
            rec = state["files"].get(dest)
            if rec and rec.get("strategy") == "create" and rec.get("sha256") == sha256(cur):
                item.action, item.new_text = "update", body
                item.detail = "harness-owned and unmodified since install"
            else:
                item.action, item.sidecar_text = "conflict", body
                item.conflicts.append("exists with different content (not harness-owned or locally modified)")

    elif strat == "json-merge":
        frag = json.loads(body, object_pairs_hook=OrderedDict)
        if cur is None:
            item.action, item.new_text = "create", dump_json(frag, 2)
        else:
            try:
                existing = json.loads(cur, object_pairs_hook=OrderedDict) if cur.strip() else OrderedDict()
            except ValueError as e:
                raise HFError("%s is not valid JSON (%s); fix it before installing" % (dest, e))
            added, conflicts = [], []
            merged = json_merge(existing, frag, [], added, conflicts, prefer_fragment=False)
            if conflicts:
                forced = json_merge(existing, frag, [], [], [], prefer_fragment=True)
                item.action = "conflict"
                item.conflicts = conflicts
                item.sidecar_text = dump_json(forced, detect_indent(cur))
            elif merged == existing:
                item.action = "unchanged"
            else:
                item.action = "merge"
                item.new_text = dump_json(merged, detect_indent(cur))
                item.detail = "; ".join(added)

    elif strat == "md-section":
        name = entry["marker"]
        block = make_block(name, body)
        if cur is None:
            item.action, item.new_text = "create", block + "\n"
        else:
            # Write the block in the file's own line ending so a CRLF file stays CRLF.
            nl = newline_of(cur)
            block = to_newline(block, nl)
            span = find_block(cur, name)
            if span is None:
                sep = "" if cur == "" else (nl if cur.endswith("\n") else nl + nl)
                item.action = "merge"
                item.new_text = cur + sep + block + nl
                item.detail = "append section %r" % name
            elif entry.get("once"):
                item.action = "unchanged"
                item.detail = "section %r kept (human-owned after first install)" % name
            elif cur[span[0]:span[1]] == block:
                item.action = "unchanged"
            else:
                item.action = "update"
                item.new_text = cur[:span[0]] + block + cur[span[1]:]
                item.detail = "replace managed section %r" % name

    elif strat == "line-append":
        want = [line for line in body.splitlines() if line.strip()]
        have = set(norm_lines(cur or ""))
        aliases = entry.get("aliases", {})
        missing = [line for line in want
                   if line.strip() not in have and not any(a.strip() in have for a in aliases.get(line, []))]
        if not missing:
            item.action = "unchanged"
        else:
            base = cur or ""
            nl = newline_of(base)  # keep a CRLF file CRLF
            if base and not base.endswith("\n"):
                base += nl
            item.action = "create" if cur is None else "merge"
            item.new_text = base + nl.join(missing) + nl
            item.detail = "add %d line(s): %s" % (len(missing), ", ".join(missing))

    if item.action in ("unchanged",) and item.mode and os.path.exists(item.real):
        want_mode = int(item.mode, 8)
        if os.stat(item.real).st_mode & 0o777 != want_mode and strat == "create":
            item.action = "update"
            item.new_text = cur
            item.detail = "fix mode to %s" % item.mode
    return item


def build_plan(repo, rung):
    if not os.path.isdir(repo):
        raise HFError("repo not found: %s" % repo)
    manifest = load_manifest(rung)
    cfg = load_config(repo)
    check_requires(manifest, cfg)
    state = load_state(repo)
    overlay, items, creates = {}, [], set()
    for e in manifest.get("files", []):
        it = plan_entry(repo, rung, e, cfg, state, overlay)
        if it.action != "skip":
            if it.strategy == "create":
                if it.real in creates or it.real in overlay:
                    raise HFError("manifest has two active entries for %s" % it.dest)
                creates.add(it.real)
            if it.new_text is not None:
                overlay[it.real] = it.new_text
        items.append(it)
    return manifest, cfg, state, items


# ---------------------------------------------------------------- writing

def atomic_write(path, text, mode=None):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hf-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        elif os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def last_audit_rung(repo):
    """rung_achieved from the most recent audit line in the ledger, or None."""
    try:
        with open(safe_dest(repo, LEDGER_FILE), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("kind") == "audit":
            r = e.get("rung_achieved")
            return r if isinstance(r, int) else None
    return None


def ladder_gate(repo, rung, cfg):
    """Refuse to install rung N until rung N-1 is proven (no skipping rungs).

    Runs the audit in-process, read-only, without executing repository code and
    without the network. A failing required check on a lower rung always
    refuses. Checks that this quick audit cannot measure (they need --run or
    the network) refuse too, unless the most recent recorded audit
    (`hf_audit.py --run --append-ledger`) shows rung N-1 achieved.
    """
    if rung <= 1:
        return
    if cfg is None:
        raise HFError("%s not found; rung %d needs the rungs below it first. "
                      "Start with: config init, then apply --rung 1" % (CONFIG_FILE, rung))
    from hf_checks.context import RepoContext
    from hf_checks.report import build_report
    rep = build_report(RepoContext(repo, run=False, offline=True), rung - 1)
    req = [c for c in rep["checks"] if c["severity"] == "required" and c["rung"] < rung]
    failing = [c for c in req if c["status"] == "fail"]
    if failing:
        raise HFError(
            "rung %d not installed: %d required check(s) on lower rungs fail (no skipping rungs):\n%s\n"
            "Fix them or re-apply the lower rung, then try again. Details: hf_audit.py --repo %s"
            % (rung, len(failing),
               "\n".join("  %s: %s" % (c["id"], c["evidence"]) for c in failing), repo))
    unmeasured = [c["id"] for c in req if c["status"] in ("not_measured", "unknown")]
    if unmeasured:
        proven = last_audit_rung(repo)
        if proven is None or proven < rung - 1:
            raise HFError(
                "rung %d not installed: rung %d is not proven yet. Not measured: %s\n"
                "Run: hf_audit.py --repo %s --run --append-ledger\n"
                "(it executes the repository's gates and smoke scripts and asks GitHub about branch "
                "protection), clear what it reports, then try again. Last recorded audit: %s."
                % (rung, rung - 1, ", ".join(unmeasured), repo,
                   "none" if proven is None else "rung %d" % proven))


def do_apply(repo, rung, on_conflict):
    manifest, cfg, state, items = build_plan(repo, rung)
    ladder_gate(repo, rung, cfg)
    conflicts = [it for it in items if it.action == "conflict"]
    if conflicts and on_conflict == "stop":
        return manifest, items, [], "aborted"
    writes = []  # (item, path, text, mode, label)
    for it in items:
        mode = int(it.mode, 8) if it.mode else None
        if it.action in ("create", "update", "merge"):
            writes.append((it, it.real, it.new_text, mode, it.action))
        elif it.action == "conflict":
            if on_conflict == "sidecar":
                writes.append((it, it.real + ".harness-new", it.sidecar_text, mode, "sidecar"))
            elif on_conflict == "replace":
                writes.append((it, it.real + ".harness-bak", read_text(it.real), None, "backup"))
                writes.append((it, it.real, it.sidecar_text, mode, "replace"))
    if not writes:
        return manifest, items, [], "noop"
    # Several entries may write one file; each planned text already includes the
    # earlier ones, so only the last write per path is needed.
    last = {}
    for i, w in enumerate(writes):
        last[w[1]] = i
    writes = [w for i, w in enumerate(writes) if last[w[1]] == i]
    # Everything is computed; now write. Backups go first by construction.
    written = []
    ver = hf_version()
    ts = now_iso()
    for it, path, text, mode, label in writes:
        atomic_write(path, text, mode)
        rel = os.path.relpath(path, os.path.realpath(repo))
        written.append(OrderedDict([("path", rel), ("action", label), ("sha256", sha256(text))]))
        if label in ("create", "update", "merge", "replace"):
            state["files"][it.dest] = OrderedDict([
                ("sha256", sha256(text)), ("rung", rung), ("strategy", it.strategy),
                ("hf_version", ver), ("ts", ts)])
    # A create-strategy file already identical to the template has no record
    # yet; record its hash too so verify and the audit can tell if it changes.
    for it in items:
        if (it.strategy == "create" and it.action == "unchanged" and it.real
                and not (state["files"].get(it.dest) or {}).get("sha256")):
            cur = read_text(it.real)
            if cur is not None:
                state["files"][it.dest] = OrderedDict([
                    ("sha256", content_sha(cur)), ("rung", rung), ("strategy", "create"),
                    ("hf_version", ver), ("ts", ts)])
    state["hf_version"] = ver
    state.move_to_end("files")
    atomic_write(safe_dest(repo, STATE_FILE), dump_json(state, 2))
    entry = OrderedDict([("ts", ts), ("kind", "install"), ("rung", rung), ("hf_version", ver),
                         ("on_conflict", on_conflict), ("files", written)])
    ledger = safe_dest(repo, LEDGER_FILE)
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return manifest, items, written, "applied"


# ---------------------------------------------------------------- verify

def resolve_token(repo, tok):
    # Each agent has its own spelling of "the repository root" in hook commands.
    for root in ("${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR",
                 "${GEMINI_PROJECT_DIR}", "$GEMINI_PROJECT_DIR",
                 "$(git rev-parse --show-toplevel)"):
        tok = tok.replace(root, repo)
    tok = os.path.expanduser(tok)
    if not os.path.isabs(tok) and "/" in tok:
        tok = os.path.join(repo, tok)
    return tok


def check_hook_command(repo, h):
    """Return None if the hook's target resolves, else a reason."""
    if h.get("type", "command") != "command":
        return None
    cmd = h.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return "empty command"
    if "args" in h:
        exe, rest = cmd, [a for a in h.get("args") or [] if isinstance(a, str)]
    else:
        try:
            toks = shlex.split(cmd)
        except ValueError as e:
            return "cannot parse command: %s" % e
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]
        if not toks:
            return "no executable in command"
        exe, rest = toks[0], toks[1:]
    path = resolve_token(repo, exe)
    if "/" in path:
        if not os.path.isfile(path):
            return "target not found: %s" % path
        if not os.access(path, os.X_OK):
            return "target not executable: %s" % path
    elif shutil.which(path) is None:
        return "executable not on PATH: %s" % path
    for tok in rest:
        if "CLAUDE_PROJECT_DIR" in tok or tok.startswith((".harness/", ".claude/", "./")):
            p = resolve_token(repo, tok)
            if not os.path.exists(p):
                return "script argument not found: %s" % p
    return None


def shipped_guard_bodies(cfg):
    """{dest: [shipped body rendered with cfg]} for create-strategy files under
    .harness/bin and .harness/hooks, across every rung's manifest."""
    out = OrderedDict()
    for r in range(1, 100):
        if not os.path.isfile(os.path.join(TEMPLATES, "rung%d" % r, "manifest.json")):
            break
        for e in load_manifest(r).get("files", []):
            if e.get("strategy") != "create" or not e.get("dest", "").startswith(GUARDED_DIRS):
                continue
            if skip_reason(e, cfg):
                continue
            src = read_text(os.path.join(TEMPLATES, "rung%d" % r, e["src"]))
            if src is None:
                continue
            try:
                body = render(src, cfg, e["src"])
            except HFError:
                continue
            out.setdefault(e["dest"], []).append(body)
    return out


def do_verify(repo, rung):
    manifest = load_manifest(rung)
    cfg = load_config(repo)
    results = []  # (status OK|MISS|SKIP, dest, message)

    def rec(ok, dest, msg):
        results.append(("OK" if ok is True else "MISS" if ok is False else ok, dest, msg))

    state = load_state(repo)
    claude_settings_used = False
    configured_agents(cfg)  # called only to raise HFError on a malformed "agents" value
    for key in manifest.get("requires_config", []):
        ok = cfg is not None and not is_empty(lookup(cfg, key))
        rec(ok, CONFIG_FILE, "%s %s" % (key, "set" if ok else "missing or empty"))

    for entry in manifest.get("files", []):
        dest = pick_dest(repo, entry)
        why = skip_reason(entry, cfg)
        if why:
            rec("SKIP", dest, why)
            continue
        try:
            real = safe_dest(repo, dest)
        except HFError as e:
            rec(False, dest, str(e))
            continue
        if entry.get("skip_if_same_file"):
            other = os.path.join(repo, entry["skip_if_same_file"])
            if os.path.exists(real) and os.path.exists(other) and os.path.samefile(real, other):
                rec("SKIP", dest, "same file as %s" % entry["skip_if_same_file"])
                continue
        cur = read_text(real)
        if cur is None:
            rec(False, dest, "missing")
            continue
        if os.path.exists(real + ".harness-new"):
            rec(False, dest, "unresolved sidecar %s.harness-new: harness version is not in effect" % dest)
        strat = entry["strategy"]
        src_text = read_text(os.path.join(TEMPLATES, "rung%d" % rung, entry["src"])) or ""
        try:
            body = render(src_text, cfg, entry["src"])
        except HFError as e:
            rec(False, dest, str(e))
            continue
        if dest == CLAUDE_SETTINGS:
            claude_settings_used = True
        if strat == "create":
            if entry.get("mode") and int(entry["mode"], 8) & 0o111 and not os.access(real, os.X_OK):
                rec(False, dest, "not executable")
            elif dest.startswith(GUARDED_DIRS):
                pass  # judged below against the shipped template, for every rung
            else:
                note = "" if strip_config_block(cur) == body else " (locally customized)"
                rec(True, dest, "present%s" % note)
        elif strat == "md-section":
            try:
                span = find_block(cur, entry["marker"])
            except HFError as e:
                rec(False, dest, str(e))
                continue
            if span is None:
                rec(False, dest, "managed section %r missing" % entry["marker"])
            else:
                rec(True, dest, "managed section %r present" % entry["marker"])
        elif strat == "line-append":
            have = set(norm_lines(cur))
            aliases = entry.get("aliases", {})
            missing = [line for line in body.splitlines() if line.strip()
                       and line.strip() not in have and not any(a.strip() in have for a in aliases.get(line, []))]
            if missing:
                rec(False, dest, "missing line(s): %s" % ", ".join(missing))
            else:
                rec(True, dest, "all lines present")
        elif strat == "json-merge":
            try:
                actual = json.loads(cur)
            except ValueError as e:
                rec(False, dest, "invalid JSON: %s" % e)
                continue
            misses = []
            json_subset(json.loads(body), actual, [], misses)
            if misses:
                for m in misses:
                    rec(False, dest, m)
            else:
                rec(True, dest, "fragment in effect")
            hooks = actual.get("hooks") if isinstance(actual, dict) else None
            if isinstance(hooks, dict):
                for event, groups in hooks.items():
                    for g in groups if isinstance(groups, list) else []:
                        for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                            if not isinstance(h, dict):
                                continue
                            why = check_hook_command(repo, h)
                            label = "hook %s: %s" % (event, h.get("command"))
                            rec(why is None, dest, label + ("" if why is None else " -> " + why))
    # Managed scripts of every rung, not only rung N: a tampered gates.sh must
    # fail `verify --rung 5` too. Compared with the skill's own templates, so a
    # rewritten hash in installed.json does not hide a change.
    bodies = shipped_guard_bodies(cfg)
    recorded = [r for r in state.get("files", {}) if r.startswith(GUARDED_DIRS)]
    for rel in sorted(set(bodies) | set(recorded)):
        try:
            text = read_text(safe_dest(repo, rel))
        except HFError:
            continue
        why = guard_problem(bodies, cfg or {}, state, rel, text)
        if why:
            rec("MODIFIED", rel, why)
        elif text is not None and rel in bodies:
            rec(True, rel, "matches the shipped script" if strip_config_block(text) in bodies[rel]
                else "customized (listed in customized_scripts)")
    if claude_settings_used:
        # Registered hooks do nothing when disableAllHooks is on. The committed
        # file governs fresh clones and CI; the local file governs this machine.
        for rel in (CLAUDE_SETTINGS, CLAUDE_LOCAL_SETTINGS):
            text = read_text(os.path.join(repo, rel))
            try:
                data = json.loads(text) if text and text.strip() else None
            except ValueError:
                if rel == CLAUDE_LOCAL_SETTINGS:
                    rec(False, rel, "invalid JSON; Claude Code cannot read it")
                continue
            if isinstance(data, dict) and data.get("disableAllHooks") is True:
                rec(False, rel, "disableAllHooks is true: no harness hook runs")
    return manifest, results


# ---------------------------------------------------------------- output

LABEL = {"create": "CREATE", "update": "UPDATE", "merge": "MERGE", "unchanged": "OK",
         "skip": "SKIP", "conflict": "CONFLICT"}


def print_items(items, out):
    width = max([len(it.dest) for it in items] + [10])
    for it in items:
        line = "  %-9s %-*s" % (LABEL[it.action], width, it.dest)
        extra = []
        if it.mode and it.action in ("create", "update"):
            extra.append("mode %s" % it.mode)
        if it.detail:
            extra.append(it.detail)
        if it.entry.get("agent"):
            a = it.entry["agent"]
            extra.append("[adapter: %s]" % (a if isinstance(a, str) else "/".join(a)))
        if extra:
            line += "  " + "; ".join(extra)
        out.write(line.rstrip() + "\n")
        for c in it.conflicts:
            out.write("  %-9s %-*s  ! %s\n" % ("", width, "", c))


def counts(items):
    c = OrderedDict()
    for it in items:
        c[it.action] = c.get(it.action, 0) + 1
    return ", ".join("%d %s" % (n, a) for a, n in c.items())


# ---------------------------------------------------------------- config and sync

CONFIG_DEFAULTS = OrderedDict([
    ("schema", 1),
    ("default_branch", "main"),
    ("agents", ["claude"]),
    ("commands", OrderedDict((k, "") for k in ("setup", "build", "test", "lint", "typecheck", "run"))),
    ("core_flow", OrderedDict([("description", ""), ("smoke_command", "")])),
    ("protected_paths", []),
    ("human_only_areas", []),
    ("max_diff_lines", 400),
    ("maintainers", []),
    ("first_loop", OrderedDict([("review", ""), ("triage", "")])),
    ("max_open_agent_prs", 3),
    ("max_fix_attempts", 2),
])
# Top-level shape: "str", "int", "strlist", or "strmap" (object of strings).
CONFIG_SHAPE = {
    "schema": "int", "default_branch": "str", "agents": "strlist",
    "commands": "strmap", "core_flow": "strmap", "protected_paths": "strlist",
    "human_only_areas": "strlist", "max_diff_lines": "int", "maintainers": "strlist",
    "first_loop": "strmap", "max_open_agent_prs": "int", "max_fix_attempts": "int",
    # Paths under .harness/bin or .harness/hooks a human approved changing;
    # verify and the audit accept them instead of reporting MODIFIED.
    "customized_scripts": "strlist",
}
SYNC_TARGETS = (
    (".harness/protected-paths.txt", ("protected_paths", "human_only_areas"),
     "# From .harness/config.json protected_paths and human_only_areas; edit there, then run sync."),
    (".harness/maintainers.txt", ("maintainers",),
     "# From .harness/config.json maintainers; edit there, then run sync."),
)


def check_shape(cfg):
    if not isinstance(cfg, dict) or cfg.get("schema") != 1:
        raise HFError('%s must be an object with "schema": 1' % CONFIG_FILE)
    for key, kind in CONFIG_SHAPE.items():
        if key not in cfg:
            continue
        v = cfg[key]
        ok = {"str": lambda: isinstance(v, str),
              "int": lambda: isinstance(v, int) and not isinstance(v, bool),
              "strlist": lambda: isinstance(v, list) and all(isinstance(x, str) for x in v),
              "strmap": lambda: isinstance(v, dict) and all(isinstance(x, str) for x in v.values()),
              }[kind]()
        if not ok:
            raise HFError("config %s must be %s" % (key, {
                "str": "a string", "int": "an integer", "strlist": "a list of strings",
                "strmap": "an object of strings"}[kind]))


def write_config(repo, cfg, indent=2):
    check_shape(cfg)
    atomic_write(safe_dest(repo, CONFIG_FILE), dump_json(cfg, indent))


def read_config_raw(repo):
    path = safe_dest(repo, CONFIG_FILE)
    text = read_text(path)
    if text is None:
        raise HFError("%s not found; run: config init --repo %s" % (CONFIG_FILE, repo))
    try:
        cfg = json.loads(text, object_pairs_hook=OrderedDict)
    except ValueError as e:
        raise HFError("%s is not valid JSON: %s" % (CONFIG_FILE, e))
    return cfg, detect_indent(text)


def config_init(repo, audit_path):
    path = safe_dest(repo, CONFIG_FILE)
    if os.path.exists(path):
        raise HFError("%s already exists; use config set to change it" % CONFIG_FILE)
    cfg = copy.deepcopy(CONFIG_DEFAULTS)
    filled = []
    if audit_path:
        try:
            with open(audit_path, encoding="utf-8") as f:
                det = json.load(f).get("detected") or {}
        except (OSError, ValueError, AttributeError) as e:
            raise HFError("cannot read audit JSON %s: %s" % (audit_path, e))
        for k, v in (det.get("commands") or {}).items():
            if k in cfg["commands"] and isinstance(v, str) and v:
                cfg["commands"][k] = v
                filled.append("commands." + k)
        if isinstance(det.get("default_branch"), str) and det["default_branch"]:
            cfg["default_branch"] = det["default_branch"]
            filled.append("default_branch")
        agents = [a for a in det.get("agents") or [] if isinstance(a, str)]
        if agents:
            cfg["agents"] = agents
            filled.append("agents")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_config(repo, cfg)
    return filled


def config_set(repo, key, raw):
    cfg, indent = read_config_raw(repo)
    try:
        value = json.loads(raw, object_pairs_hook=OrderedDict)
    except ValueError:
        value = raw
    parts = key.split(".")
    if not all(parts):
        raise HFError("bad key %r" % key)
    if parts[0] not in CONFIG_SHAPE:
        raise HFError("unknown top-level key %r (known: %s)" % (parts[0], ", ".join(CONFIG_SHAPE)))
    cur = cfg
    for part in parts[:-1]:
        if part not in cur:
            cur[part] = OrderedDict()
        if not isinstance(cur[part], dict):
            raise HFError("%s is not an object" % part)
        cur = cur[part]
    cur[parts[-1]] = value
    write_config(repo, cfg, indent)
    return value


def sync_block(cfg, keys, note):
    lines = []
    for k in keys:
        for v in cfg.get(k) or []:
            v = str(v).strip()
            if v and v not in lines:
                lines.append(v)
    return "%s\n%s\n%s%s\n" % (SYNC_START, note, "".join(l + "\n" for l in lines), SYNC_END)


def synced_text(cur, block):
    if SYNC_START in cur:
        if cur.count(SYNC_START) != 1 or cur.count(SYNC_END) != 1:
            raise HFError("malformed %s block" % SYNC_START)
        m = SYNC_RE.search(cur)
        if not m:
            raise HFError("malformed %s block" % SYNC_START)
        lead = "\n" if m.group(0).startswith("\n") else ""
        return cur[:m.start()] + lead + block + cur[m.end():]
    if cur and not cur.endswith("\n"):
        cur += "\n"
    return cur + "\n" + block


def do_sync(repo, write=True):
    """Return [(status, path, message)]; status is updated|unchanged|stale|absent."""
    cfg = load_config(repo)
    if cfg is None:
        raise HFError("%s not found; run: config init --repo %s" % (CONFIG_FILE, repo))
    check_shape(cfg)
    out = []
    for rel, keys, note in SYNC_TARGETS:
        path = safe_dest(repo, rel)
        cur = read_text(path)
        if cur is None:
            out.append(("absent", rel, "not installed yet"))
            continue
        new = synced_text(cur, sync_block(cfg, keys, note))
        n = sum(len(cfg.get(k) or []) for k in keys)
        if new == cur:
            out.append(("unchanged", rel, "%d entr%s from config" % (n, "y" if n == 1 else "ies")))
        elif write:
            atomic_write(path, new)
            out.append(("updated", rel, "%d entr%s from config" % (n, "y" if n == 1 else "ies")))
        else:
            out.append(("stale", rel, "config block does not match config; run sync"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hf_install.py", description="harness-factory installer")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("plan", "apply", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--rung", required=True, type=int)
        p.add_argument("--json", action="store_true")
        if name == "apply":
            p.add_argument("--on-conflict", choices=("stop", "sidecar", "replace"), default="stop")
    p = sub.add_parser("sync")
    p.add_argument("--repo", required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("config")
    csub = p.add_subparsers(dest="config_cmd")
    for name in ("init", "set", "show"):
        cp = csub.add_parser(name)
        cp.add_argument("--repo", required=True)
        cp.add_argument("--json", action="store_true")
        if name == "init":
            cp.add_argument("--from-audit", metavar="AUDIT.json")
        if name == "set":
            cp.add_argument("key")
            cp.add_argument("value")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_usage(sys.stderr)
        return 2
    repo = os.path.abspath(args.repo)
    out = sys.stdout
    try:
        if args.cmd == "config":
            if args.config_cmd == "init":
                filled = config_init(repo, args.from_audit)
                out.write("Wrote %s%s\n" % (CONFIG_FILE, " (from audit: %s)" % ", ".join(filled) if filled else ""))
            elif args.config_cmd == "set":
                v = config_set(repo, args.key, args.value)
                out.write("%s = %s\n" % (args.key, json.dumps(v, ensure_ascii=False)))
            elif args.config_cmd == "show":
                cfg, _ = read_config_raw(repo)
                out.write(dump_json(cfg, 2))
            else:
                ap.print_usage(sys.stderr)
                return 2
            return 0

        if args.cmd == "sync":
            res = do_sync(repo)
            if args.json:
                json.dump([OrderedDict([("status", a), ("path", b), ("message", c)]) for a, b, c in res], out, indent=2)
                out.write("\n")
            else:
                for a, b, c in res:
                    out.write("  %-9s %s  %s\n" % (a, b, c))
            return 0

        if args.cmd == "plan":
            manifest, _, _, items = build_plan(repo, args.rung)
            n_conf = sum(1 for it in items if it.action == "conflict")
            pending = [it for it in items if it.action in ("create", "update", "merge", "conflict")]
            if args.json:
                json.dump(OrderedDict([("command", "plan"), ("repo", repo), ("rung", args.rung),
                                       ("noop", not pending), ("conflicts", n_conf),
                                       ("items", [it.as_dict() for it in items])]), out, indent=2)
                out.write("\n")
            else:
                out.write("harness-factory plan: rung %d (%s) -> %s\n\n" % (
                    args.rung, manifest.get("name", ""), repo))
                print_items(items, out)
                out.write("\n%s\n" % counts(items))
                if not pending:
                    out.write("Nothing to do: already installed and up to date.\n")
                elif n_conf:
                    out.write("%d conflict(s). apply will stop; rerun apply with --on-conflict sidecar "
                              "(write <dest>.harness-new) or replace (back up to <dest>.harness-bak).\n" % n_conf)
                else:
                    out.write("Run apply to write these changes.\n")
            return 1 if n_conf else 0

        if args.cmd == "apply":
            manifest, items, written, outcome = do_apply(repo, args.rung, args.on_conflict)
            synced = do_sync(repo) if outcome != "aborted" else []
            if args.json:
                json.dump(OrderedDict([("command", "apply"), ("repo", repo), ("rung", args.rung),
                                       ("outcome", outcome), ("written", written),
                                       ("sync", [OrderedDict([("status", a), ("path", b), ("message", c)])
                                                 for a, b, c in synced]),
                                       ("items", [it.as_dict() for it in items])]), out, indent=2)
                out.write("\n")
            else:
                out.write("harness-factory apply: rung %d (%s) -> %s\n\n" % (
                    args.rung, manifest.get("name", ""), repo))
                print_items(items, out)
                if outcome == "aborted":
                    out.write("\nABORTED: conflicts above; nothing was written. Use --on-conflict sidecar|replace.\n")
                elif outcome == "noop":
                    out.write("\nNo-op: everything is already installed and up to date. Nothing written.\n")
                else:
                    out.write("\nWrote %d file(s):\n" % len(written))
                    for w in written:
                        out.write("  %-8s %s\n" % (w["action"], w["path"]))
                    out.write("Recorded in %s and %s. Next: verify.\n" % (STATE_FILE, LEDGER_FILE))
                for a, b, c in synced:
                    if a != "absent":
                        out.write("sync: %-9s %s  %s\n" % (a, b, c))
            return 1 if outcome == "aborted" else 0

        if args.cmd == "verify":
            manifest, results = do_verify(repo, args.rung)
            if load_config(repo) is not None:
                for a, b, c in do_sync(repo, write=False):
                    if a != "absent":
                        results.append(("MISS" if a == "stale" else "OK", b, "config block: " + c))
            bad = [r for r in results if r[0] in ("MISS", "MODIFIED")]
            if args.json:
                json.dump(OrderedDict([("command", "verify"), ("repo", repo), ("rung", args.rung),
                                       ("ok", not bad),
                                       ("results", [OrderedDict([("status", st), ("dest", d), ("message", m)])
                                                    for st, d, m in results])]), out, indent=2)
                out.write("\n")
            else:
                out.write("harness-factory verify: rung %d (%s) -> %s\n\n" % (
                    args.rung, manifest.get("name", ""), repo))
                width = max([len(d) for _, d, _ in results] + [10])
                for st, d, m in results:
                    out.write("  %-4s %-*s  %s\n" % (st, width, d, m))
                out.write("\n%s: %d check(s), %d miss(es)\n" % ("PASS" if not bad else "FAIL", len(results), len(bad)))
            return 1 if bad else 0
    except HFError as e:
        if args.json:
            json.dump({"command": args.cmd, "error": str(e)}, out, indent=2)
            out.write("\n")
        sys.stderr.write("hf_install: error: %s\n" % e)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
