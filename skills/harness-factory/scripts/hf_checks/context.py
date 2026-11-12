"""Read-only view of the audited repo.

Everything here only reads. Git is invoked with options that stop it from
running repo-configured programs (fsmonitor) or taking optional locks, so
auditing an untrusted repo never executes its code or touches its files.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from typing import Dict, List, Optional, Tuple

# Directory names that are vendored, generated or tool caches. Skipped both in
# the git listing and in the filesystem walk.
SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "out", "target",
    ".venv", "venv", "__pycache__", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".next", ".nuxt", ".svelte-kit",
    ".turbo", ".parcel-cache", ".cache", "coverage", ".gradle", ".idea",
    ".terraform", "bower_components", "Pods", "DerivedData", "site-packages",
    ".eggs", "_build", ".yarn", ".pnpm-store", ".bundle", "htmlcov",
}

MAX_FILES = 50000          # listing cap; beyond this the repo is sampled
MAX_READ_BYTES = 512 * 1024  # per-file read cap for content scans

GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]


def _skip_path(rel: str) -> bool:
    parts = rel.split("/")
    for p in parts[:-1]:
        if p in SKIP_DIRS or p.endswith(".egg-info"):
            return True
    return False


class RepoContext:
    def __init__(self, root: str, run: bool = False, offline: bool = False,
                 run_timeout: int = 900):
        self.root = os.path.realpath(root)
        self.run = run
        self.offline = offline
        self.run_timeout = run_timeout
        self._text_cache: Dict[str, Optional[str]] = {}
        self._json_cache: Dict[str, Tuple[object, Optional[str]]] = {}
        self.git_available = shutil.which("git") is not None
        self.is_git = False
        self.files: List[str] = []
        self.tracked: set = set()
        self.modes: Dict[str, str] = {}
        self.listing_truncated = False
        self.listing_source = "filesystem"
        self._list_files()
        self._detected = None

    # ---------------------------------------------------------------- git
    def git(self, *args: str, timeout: int = 20) -> Optional[Tuple[int, str]]:
        if not self.git_available:
            return None
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_PAGER": "cat", "LC_ALL": "C"})
        try:
            p = subprocess.run(["git"] + GIT_SAFE + ["-C", self.root] + list(args),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def _list_files(self) -> None:
        r = self.git("rev-parse", "--show-toplevel")
        if r and r[0] == 0:
            top = os.path.realpath(r[1].strip())
            self.is_git = True
            # Only use the git listing when the audited path is the work tree
            # root; a subdirectory audit falls back to the filesystem walk.
            if top == self.root:
                staged = self.git("ls-files", "-s", "-z", timeout=60)
                others = self.git("ls-files", "-z", "--others", "--exclude-standard", timeout=60)
                if staged and staged[0] == 0:
                    for entry in staged[1].split("\0"):
                        if not entry or "\t" not in entry:
                            continue
                        meta, path = entry.split("\t", 1)
                        mode = meta.split(" ", 1)[0]
                        self.modes[path] = mode
                        self.tracked.add(path)
                    paths = list(self.tracked)
                    if others and others[0] == 0:
                        paths += [p for p in others[1].split("\0") if p]
                    paths = sorted(p for p in set(paths) if not _skip_path(p))
                    if len(paths) > MAX_FILES:
                        self.listing_truncated = True
                        paths = paths[:MAX_FILES]
                    self.files = paths
                    self.listing_source = "git"
                    return
        self._walk()

    def _walk(self) -> None:
        out: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS and not d.endswith(".egg-info"))
            rel_dir = os.path.relpath(dirpath, self.root)
            for f in sorted(filenames):
                rel = f if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/" + f
                out.append(rel)
                if len(out) >= MAX_FILES:
                    self.listing_truncated = True
                    self.files = out
                    return
        self.files = out

    # ---------------------------------------------------------------- paths
    def abspath(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def inside(self, abs_path: str) -> bool:
        real = os.path.realpath(abs_path)
        return real == self.root or real.startswith(self.root + os.sep)

    def exists(self, rel: str) -> bool:
        p = self.abspath(rel)
        return os.path.exists(p) and self.inside(p)

    def isfile(self, rel: str) -> bool:
        p = self.abspath(rel)
        return os.path.isfile(p) and self.inside(p)

    def isdir(self, rel: str) -> bool:
        p = self.abspath(rel)
        return os.path.isdir(p) and self.inside(p)

    def listdir(self, rel: str) -> List[str]:
        if not self.isdir(rel):
            return []
        try:
            return sorted(os.listdir(self.abspath(rel)))
        except OSError:
            return []

    def first_existing(self, *rels: str) -> Optional[str]:
        for r in rels:
            if self.isfile(r):
                return r
        return None

    def find_ci(self, *rels: str) -> Optional[str]:
        """Case-insensitive lookup of a file path relative to root."""
        hit = self.first_existing(*rels)
        if hit:
            return hit
        wanted = {r.lower() for r in rels}
        for f in self.files:
            if f.lower() in wanted:
                return f
        return None

    # ---------------------------------------------------------------- read
    def read_text(self, rel: str, max_bytes: int = MAX_READ_BYTES) -> Optional[str]:
        if rel in self._text_cache:
            return self._text_cache[rel]
        p = self.abspath(rel)
        text = None
        # Never follow a symlink out of the repo.
        if os.path.isfile(p) and self.inside(p):
            try:
                with open(p, "rb") as fh:
                    data = fh.read(max_bytes)
                if b"\0" not in data[:8192]:
                    text = data.decode("utf-8", "replace")
            except OSError:
                text = None
        self._text_cache[rel] = text
        return text

    def lines(self, rel: str) -> List[str]:
        t = self.read_text(rel)
        return t.splitlines() if t is not None else []

    def load_json(self, rel: str) -> Tuple[object, Optional[str]]:
        """Return (data, error). data is None when absent or invalid."""
        if rel in self._json_cache:
            return self._json_cache[rel]
        if not self.isfile(rel):
            res: Tuple[object, Optional[str]] = (None, "missing")
        else:
            t = self.read_text(rel, max_bytes=4 * 1024 * 1024)
            if t is None:
                res = (None, "unreadable")
            else:
                try:
                    res = (json.loads(t), None)
                except ValueError as e:
                    res = (None, "invalid JSON: %s" % e)
        self._json_cache[rel] = res
        return res

    def meaningful_lines(self, rel: str) -> List[Tuple[int, str]]:
        """Non-empty, non-comment lines with 1-based line numbers."""
        out = []
        for i, line in enumerate(self.lines(rel), 1):
            s = line.strip()
            if s and not s.startswith("#"):
                out.append((i, s))
        return out

    # ---------------------------------------------------------------- modes
    def executable(self, rel: str) -> Tuple[bool, str]:
        """Is the file executable where it matters (the committed mode)?"""
        if not self.isfile(rel):
            return False, "%s missing" % rel
        on_disk = bool(os.stat(self.abspath(rel)).st_mode & stat.S_IXUSR)
        if rel in self.modes:
            committed = self.modes[rel] == "100755"
            if not committed:
                return False, "%s is committed as mode %s (not executable in a fresh clone)" % (
                    rel, self.modes[rel])
            return True, "%s executable" % rel
        if on_disk:
            return True, "%s executable%s" % (rel, self.uncommitted(rel))
        return False, "%s is not executable" % rel

    def uncommitted(self, rel: str) -> str:
        if self.is_git and self.listing_source == "git" and rel not in self.tracked:
            return " (not committed yet; cloud runs will not see it)"
        return ""

    # ---------------------------------------------------------------- config
    def harness_config(self) -> dict:
        data, _ = self.load_json(".harness/config.json")
        return data if isinstance(data, dict) else {}

    def settings(self) -> Tuple[Optional[dict], Optional[str]]:
        data, err = self.load_json(".claude/settings.json")
        if err:
            return None, err
        if not isinstance(data, dict):
            return None, "top level is not an object"
        return data, None

    @property
    def agents(self) -> List[str]:
        """Agents the harness targets: config `agents`, default ["claude"]."""
        val = self.harness_config().get("agents")
        if isinstance(val, list):
            names = [a for a in val if isinstance(a, str) and a.strip()]
            if names:
                return names
        return ["claude"]

    @property
    def detected(self) -> dict:
        if self._detected is None:
            from .detect import detect
            self._detected = detect(self)
        return self._detected

    @property
    def default_branch(self) -> str:
        return self.detected.get("default_branch", "")


def has_marker(ctx: RepoContext, rel: str, marker: str = "HARNESS_STUB") -> bool:
    """True when a comment line starts with the marker (e.g. `# HARNESS_STUB: ...`).

    Only a leading comment counts, because the real gates.sh mentions the
    marker in code to detect stub gates.
    """
    t = ctx.read_text(rel)
    return t is not None and re.search(r"(?m)^\s*(?:#|//)\s*" + re.escape(marker), t) is not None
