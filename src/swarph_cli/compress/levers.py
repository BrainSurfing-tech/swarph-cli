"""The two compression levers. archival_split is mechanical + lossless;
shorthand is the model call (lossy, bounded to index-over-source)."""
from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass
class ArchivalResult:
    live: str       # head + pointer (stays auto-loaded)
    archive: str    # cold tail (moves to <name>)


def archival_split(source: str, boundary: str, archive_name: str) -> ArchivalResult | None:
    """Split at the first line matching `boundary`. Returns None (refuse) if no
    boundary line is found — never infer a split on a messy file. Lossless:
    live(head) + archive(tail) reconstruct the source minus the pointer line."""
    lines = source.splitlines(keepends=True)
    cut = next((i for i, ln in enumerate(lines) if re.search(boundary, ln)), None)
    if cut is None:
        return None
    head = "".join(lines[:cut])
    tail = "".join(lines[cut:])
    pointer = (f"\n## Archived\n\nHistorical content below the boundary moved to "
               f"`{archive_name}` (loaded on demand). Live content is above.\n")
    return ArchivalResult(live=head.rstrip() + "\n" + pointer, archive=tail)
