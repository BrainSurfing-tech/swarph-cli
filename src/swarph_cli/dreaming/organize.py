"""ORGANIZE. The checker already existed and was read-only; R5 needs the trim."""
from __future__ import annotations
from pathlib import Path

BUDGET = 24985   # BYTES. GC6 -- never len(str).
# Trim to a TARGET BELOW the ceiling, not to the ceiling. LIVED 2026-09-03:
# trimming to 24,972 (13 bytes of headroom) is "clean" and the very next index
# line puts it straight back over. A pass that lands one byte under has not
# organised anything; it has queued the same breach for tomorrow.
TARGET = int(BUDGET * 0.94)   # ~1,500 bytes of headroom

# Claude Code loads MEMORY.md at session start and TRUNCATES after line 200
# (#938). Same headroom rule as bytes: land under the target, not the ceiling.
LINE_LIMIT = 200   # harness truncation line
LINE_TARGET = int(LINE_LIMIT * 0.94)  # 188


def organize(clone: Path) -> dict:
    import subprocess, sys
    # --mem-dir, not an env override: the checker's own path is hardcoded and its
    # failure is on stderr, so a wrong pointer reads as a clean corpus.
    check = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "index_check.py"), "--mem-dir=%s" % clone],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    findings = [l for l in (check.stdout + check.stderr).split("\n") if l.strip()]
    idx = clone / "MEMORY.md"
    if not idx.exists():
        return {"findings": findings, "index_bytes_before": 0,
                "index_bytes_after": 0, "trimmed": [],
                "index_lines_before": 0, "index_lines_after": 0}
    raw = idx.read_bytes()
    before = len(raw)
    lines = idx.read_text(encoding="utf-8").split("\n")
    lines_before = len(lines)
    trimmed = []
    # Two independent limits (#938 / droplet plan-review): bytes OR lines may
    # bind first depending on line length. Trim when either ceiling is breached;
    # land under BOTH targets.
    if before > BUDGET or lines_before > LINE_LIMIT:
        while lines and (
                len("\n".join(lines).encode("utf-8")) > TARGET
                or len(lines) > LINE_TARGET):
            trimmed.append(lines.pop())
        idx.write_text("\n".join(lines), encoding="utf-8")
    after_text = idx.read_text(encoding="utf-8") if idx.exists() else ""
    return {"findings": findings, "index_bytes_before": before,
            "index_bytes_after": len(idx.read_bytes()),
            "index_lines_before": lines_before,
            "index_lines_after": len(after_text.split("\n")) if after_text or idx.exists() else 0,
            "trimmed": [t for t in trimmed if t.strip()]}
