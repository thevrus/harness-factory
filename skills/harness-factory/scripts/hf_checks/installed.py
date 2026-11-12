"""Install state (.harness/installed.json) shared by the installer and the audit.

The installer records a sha256 for every file it writes. For the create
strategy that hash covers the file with the sync-owned config block removed,
so `sync` does not make a managed file look modified. A record without a
sha256 (older installs) means "not checked".
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

STATE_FILE = ".harness/installed.json"
SYNC_START = "# harness:config:start"
SYNC_END = "# harness:config:end"
SYNC_RE = re.compile(r"\n?^%s[ \t]*\n.*?^%s[ \t]*(?:\n|\Z)" % (re.escape(SYNC_START), re.escape(SYNC_END)),
                     re.M | re.S)
# Managed executables whose edits change what the harness enforces.
GUARDED_DIRS = (".harness/bin/", ".harness/hooks/")
MODIFIED_HOW = "modified since install; re-run the installer or review the change"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_config_block(text):
    return SYNC_RE.sub("", text, count=1) if text and SYNC_START in text else text


def content_sha(text: str) -> str:
    return sha256(strip_config_block(text))


def recorded_sha(state, rel: str) -> Optional[str]:
    """The create-strategy hash recorded for rel, or None when not checkable."""
    files = state.get("files") if isinstance(state, dict) else None
    rec = files.get(rel) if isinstance(files, dict) else None
    if not isinstance(rec, dict) or rec.get("strategy") != "create":
        return None
    h = rec.get("sha256")
    return h if isinstance(h, str) and h else None


def is_modified(state, rel: str, text: Optional[str]) -> Optional[bool]:
    """True if rel differs from its recorded hash; None if there is no record or no file."""
    want = recorded_sha(state, rel)
    if want is None or text is None:
        return None
    return content_sha(text) != want


def ctx_state(ctx) -> dict:
    """Parsed installed.json from an audit RepoContext ({} when absent or invalid)."""
    text = ctx.read_text(STATE_FILE, max_bytes=4 * 1024 * 1024)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def guard_problem(bodies, cfg, state, rel: str, text: Optional[str]) -> Optional[str]:
    """Why a managed script under .harness/bin or .harness/hooks is not the shipped one, or None.

    bodies maps dest -> [shipped template rendered with the repo config].
    The repo-local hash is not trusted on its own (it can be rewritten along
    with the script): the file must match the skill's own template. A
    deliberate, human-approved change is declared by listing the path in
    config `customized_scripts`. Without a renderable template (no config,
    unknown path) it falls back to the recorded hash.
    """
    if text is None or not rel.startswith(GUARDED_DIRS):
        return None
    custom = cfg.get("customized_scripts") if isinstance(cfg, dict) else None
    if isinstance(custom, list) and rel in custom:
        return None
    if rel not in bodies:
        return MODIFIED_HOW if is_modified(state, rel, text) else None
    if strip_config_block(text) in bodies[rel]:
        return None
    from . import HF_VERSION
    inst = state.get("hf_version") if isinstance(state, dict) else None
    if inst and inst != HF_VERSION:
        return ("differs from the shipped script; installed with hf %s, audited with hf %s; "
                "re-run the installer" % (inst, HF_VERSION))
    return ("differs from the shipped script; re-run the installer or review the change "
            "(an approved change is listed in customized_scripts in .harness/config.json)")


_BODIES = {}


def ctx_guard_problem(ctx, rel: str) -> Optional[str]:
    """Audit view of guard_problem for one repo-relative path."""
    if not rel.startswith(GUARDED_DIRS):
        return None
    cfg = ctx.harness_config()
    if ctx.root not in _BODIES:
        try:
            import hf_install  # the installer owns template rendering
            _BODIES[ctx.root] = hf_install.shipped_guard_bodies(cfg if cfg else None)
        except Exception:  # templates unavailable: fall back to recorded hashes
            _BODIES[ctx.root] = {}
    return guard_problem(_BODIES[ctx.root], cfg, ctx_state(ctx), rel, ctx.read_text(rel))
