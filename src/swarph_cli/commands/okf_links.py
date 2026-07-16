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

# Single pinned grammar, one compiled pattern, two named groups:
#   'wiki' -> ![[x]] or [[x]], target up to the first '#', '|', or ']'.
#   'md'   -> [text](path.md), a standard-markdown link to a .md file.
# Kept as ONE regex (not two matched separately) so the grammar can never
# drift between a "capture" copy and a "scan" copy — there is only one copy.
_LINK = re.compile(
    r"!?\[\[\s*(?P<wiki>[^\]|#]+?)\s*(?:[#|][^\]]*)?\]\]"
    r"|\[[^\]]*\]\(\s*(?P<md>[^)\s]+\.md)\s*\)"
)


def parse_okf_links(text: str) -> list[str]:
    """Extract OKF edge targets from `text`, in document order, de-duped."""
    if not text:
        return []
    hits = []
    for m in _LINK.finditer(text):
        if m.lastgroup == "wiki":
            hits.append(m.group("wiki").strip())
        elif m.lastgroup == "md":
            hits.append(m.group("md").strip())
    return list(dict.fromkeys(hits))
