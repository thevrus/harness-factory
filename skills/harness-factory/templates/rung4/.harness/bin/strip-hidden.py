#!/usr/bin/env python3
"""Strip content a human reader cannot see before untrusted text reaches a prompt.

Usage: strip-hidden.py [--max-chars N] [INPUT]   (reads stdin when INPUT is omitted)

Removes HTML comments, invisible format characters (zero-width, bidi controls,
soft hyphen), Unicode tag characters and variation-selector supplements (both
used for "ASCII smuggling"), and elements marked hidden. Writes the cleaned text
to stdout and a one-line count of what was removed to stderr.

This is defense in depth only. Sanitizers get bypassed; the real controls are
that the job reading this text has no write permission and its output is
validated by a separate job.
"""
import argparse
import re
import sys
import unicodedata

COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
HIDDEN_EL_RE = re.compile(
    r"<(\w+)[^>]*\b(?:hidden|aria-hidden\s*=\s*[\"']?true|style\s*=\s*[\"'][^\"']*display\s*:\s*none)[^>]*>.*?</\1\s*>",
    re.S | re.I,
)
# Markdown image alt text and link titles are invisible in rendered views.
IMG_ALT_RE = re.compile(r"!\[[^\]]*\]\(")


def is_invisible(ch):
    cp = ord(ch)
    if ch in "\n\t\r":
        return False
    if 0xE0000 <= cp <= 0xE007F:  # tag characters
        return True
    if 0xE0100 <= cp <= 0xE01EF:  # variation selectors supplement
        return True
    cat = unicodedata.category(ch)
    # Cf covers zero-width space/joiners, bidi embeddings, overrides and
    # isolates, soft hyphen, word joiner and BOM. Cc covers other controls.
    return cat in ("Cf", "Cc")


def clean(text, max_chars):
    counts = {"html_comments": 0, "hidden_elements": 0, "invisible_chars": 0, "image_alts": 0, "truncated": 0}
    text, counts["html_comments"] = COMMENT_RE.subn("", text)
    text, counts["hidden_elements"] = HIDDEN_EL_RE.subn("", text)
    text, counts["image_alts"] = IMG_ALT_RE.subn("![](", text)
    out = []
    for ch in text:
        if is_invisible(ch):
            counts["invisible_chars"] += 1
        else:
            out.append(ch)
    text = unicodedata.normalize("NFKC", "".join(out))
    if max_chars and len(text) > max_chars:
        counts["truncated"] = len(text) - max_chars
        text = text[:max_chars] + "\n\n[truncated by strip-hidden.py: %d characters removed]\n" % counts["truncated"]
    return text, counts


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", nargs="?", help="file to clean (default: stdin)")
    ap.add_argument("--max-chars", type=int, default=20000, help="truncate after N characters (0 = no limit)")
    args = ap.parse_args()
    if args.input:
        with open(args.input, encoding="utf-8", errors="replace") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    cleaned, counts = clean(text, args.max_chars)
    sys.stdout.write(cleaned)
    sys.stderr.write("strip-hidden: " + " ".join("%s=%d" % kv for kv in counts.items()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
