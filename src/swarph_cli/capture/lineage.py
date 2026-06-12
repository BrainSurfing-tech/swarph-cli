"""Append-only provenance lineage (spec §4.4 / §5 / §6).

Answers "was this birth SANCTIONED?" (an edge property), NOT "is this the same
self?" (continuity — that's the HEAD, a separate deferred layer). Genesis records
are self (parent=null); mitosis records name the spawning parent. The crypto
counter-signature is DEFERRED (auth-ladder per-cell keys); the schema ships the
seam NOW as signed:false + sig/parent_sig null so signing is purely additive.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from typing import Optional

from swarph_shared.cell import Cell

from swarph_cli.capture import paths
from swarph_cli.cell import read_starter_prompt


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def workspace_fingerprint(cell: Cell) -> str:
    """sha256(cwd + cell.yaml bytes + starter text) — deterministic per spec §5."""
    h = hashlib.sha256()
    h.update(str(cell.cwd).encode("utf-8"))
    if cell.source_path is not None and cell.source_path.exists():
        h.update(cell.source_path.read_bytes())
    starter = read_starter_prompt(cell)
    if starter:
        h.update(starter.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def lineage_exists(role: str) -> bool:
    return paths.lineage_path(role).exists()


def append_lineage_event(role: str, event: dict) -> None:
    """Append one JSON event as a line to lineage/<role>.jsonl (append-only)."""
    target = paths.lineage_path(role)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True)
    with open(target, "a", encoding="utf-8") as fp:
        fp.write(line + "\n")
        fp.flush()
        os.fsync(fp.fileno())


def _base_event(cell: Cell, *, session_id: str, cursor_path: Optional[str]) -> dict:
    return {
        "session_id": session_id,
        "when": _now_iso(),
        "where": {"cwd": str(cell.cwd), "host": socket.gethostname()},
        "workspace_fingerprint": workspace_fingerprint(cell),
        "cursor_path": cursor_path,
        "signed": False,
        "sig": None,
        "parent_sig": None,
    }


def record_genesis(cell: Cell, *, session_id: str, cursor_path: Optional[str]) -> None:
    event = _base_event(cell, session_id=session_id, cursor_path=cursor_path)
    event.update({
        "cell": cell.role,
        "kind": "genesis",
        "parent": None,
        "parent_session_id": None,
    })
    append_lineage_event(cell.role, event)


def record_mitosis(
    cell: Cell,
    *,
    child_role: str,
    parent_role: str,
    child_session_id: str,
    parent_session_id: Optional[str],
    cursor_path: Optional[str],
) -> None:
    event = _base_event(cell, session_id=child_session_id, cursor_path=cursor_path)
    event.update({
        "cell": child_role,
        "kind": "mitosis",
        "parent": parent_role,
        "parent_session_id": parent_session_id,
    })
    append_lineage_event(child_role, event)
