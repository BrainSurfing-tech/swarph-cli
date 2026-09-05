"""Incremental read over session transcripts.

MEASURED 2026-09-03, largest transcript on this box: 1.26 GB / 380,507 records.
Filtering to user+assistant is a 2.6x cut (1.26 GB -> 488 MB) -- NOT enough on
its own. Bytes are dominated by `file-history-snapshot` (49.4%, from only 5,381
records); record counts point at `attachment` (104,040 records, 9.4% of bytes)
and are misleading. The byte-offset cursor is the lever that makes this cheap.
"""
from __future__ import annotations
import json
from pathlib import Path

KEEP = {"user", "assistant"}


def read_new(dirpath: Path, cursor_path: Path, max_bytes: int = 50_000_000):
    cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
    new_cursor, records, budget = dict(cursor), [], max_bytes
    for f in sorted(dirpath.glob("*.jsonl")):
        size = f.stat().st_size
        start = cursor.get(f.name, 0)
        if start > size:
            start = 0          # truncated or rotated -- never seek past EOF
        with f.open("r", errors="replace") as fh:
            fh.seek(start)
            while budget > 0:
                line_start = fh.tell()
                line = fh.readline()
                if not line:
                    break
                budget -= len(line)
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") in KEEP:
                    # PROVENANCE, NOT JUST CONTENT. A bare record cannot be
                    # attributed, and R2 + the talk's VERSIONING guardrail
                    # both require naming WHICH SESSION an update came from.
                    # `_src` is namespaced so it cannot collide with a
                    # transcript key.
                    rec["_src"] = {"file": f.name, "session_id": f.stem,
                                   "offset": line_start, "end": fh.tell()}
                    records.append(rec)
            new_cursor[f.name] = fh.tell()
    return records, new_cursor
