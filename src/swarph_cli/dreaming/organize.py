"""ORGANIZE. The checker already existed and was read-only; R5 needs the trim."""
from __future__ import annotations
from pathlib import Path

BUDGET = 24985   # BYTES. GC6 -- never len(str).
# Trim to a TARGET BELOW the ceiling, not to the ceiling. LIVED 2026-09-03:
# trimming to 24,972 (13 bytes of headroom) is "clean" and the very next index
# line puts it straight back over. A pass that lands one byte under has not
# organised anything; it has queued the same breach for tomorrow.
TARGET = int(BUDGET * 0.94)   # ~1,500 bytes of headroom


def organize(clone: Path) -> dict:
    import subprocess, sys
    # --mem-dir, not an env override: the checker's own path is hardcoded and its
    # failure is on stderr, so a wrong pointer reads as a clean corpus.
    check = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "index_check.py"), "--mem-dir=%s" % clone],
        capture_output=True, text=True)
    findings = [l for l in (check.stdout + check.stderr).split("\n") if l.strip()]
    idx = clone / "MEMORY.md"
    if not idx.exists():
        return {"findings": findings, "index_bytes_before": 0,
                "index_bytes_after": 0, "trimmed": []}
    before = len(idx.read_bytes())
    trimmed = []
    if before > BUDGET:
        lines = idx.read_text(encoding="utf-8").split("\n")
        # Trim from the END: the index is roughly oldest-first, and the head
        # carries the starred entries. ponytail: crude ordering, replace with a
        # recency/star rank if trims start dropping things that matter.
        while len("\n".join(lines).encode("utf-8")) > TARGET and lines:
            trimmed.append(lines.pop())
        idx.write_text("\n".join(lines), encoding="utf-8")
    return {"findings": findings, "index_bytes_before": before,
            "index_bytes_after": len(idx.read_bytes()),
            "trimmed": [t for t in trimmed if t.strip()]}
