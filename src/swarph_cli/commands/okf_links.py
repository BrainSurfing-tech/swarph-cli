"""Shared OKF link-grammar parser — the one true `[[link]]`/markdown-link extractor.

Reused across the OKF traversal brain: `swarph timeline` (this sub-project),
and later `swarph memory` + the `swarph brain` walker. The grammar is PINNED
(design review, droplet 2026-07-15) so the same edge is read identically
everywhere — a traversal brain is only as trustworthy as its least-deterministic
edge, so link parsing must be exact, not naive.

Grammar (target in brackets):
  [[slug]]          -> slug
  [[slug|alias]]    -> slug          (alias is display text, not the target)
  [[slug#heading]]  -> slug          (heading is an intra-page anchor)
  ![[embed]]        -> embed         (transclusion is a reference = an edge)
  [text](path.md)   -> path.md       (standard-markdown link to a .md file)
Order-preserving de-dupe; non-.md markdown links (http, images) are ignored.
"""
from __future__ import annotations

import re

# ![[x]] or [[x]] — capture the target up to the first '#', '|', or ']'.
_WIKI = re.compile(r"!?\[\[\s*([^\]|#]+?)\s*(?:[#|][^\]]*)?\]\]")
# [text](path.md) — capture a .md path target (ignore http/images/other extensions).
_MDLINK = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+\.md)\s*\)")


def parse_okf_links(text: str) -> list:
    """Extract OKF edge targets from `text`, in document order, de-duped."""
    if not text:
        return []
    hits = []
    for m in re.finditer(r"!?\[\[\s*[^\]|#]+?\s*(?:[#|][^\]]*)?\]\]|\[[^\]]*\]\(\s*[^)\s]+\.md\s*\)", text):
        frag = m.group(0)
        w = _WIKI.match(frag)
        if w:
            hits.append(w.group(1).strip())
            continue
        d = _MDLINK.match(frag)
        if d:
            hits.append(d.group(1).strip())
    return list(dict.fromkeys(hits))
