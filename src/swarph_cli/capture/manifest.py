"""Capture manifest (spec §5) — the revival-kit index + live-pin + reserved HEAD.

head.jsonl_offset / sha256 / last_compact_summary_offset are reserved for the
DEFERRED HEAD/continuity checkpoint layer — written null here, never advanced.
head.live_pin_holder is the stored flag swarph-cell-verify PROBES around (never
trusts): a present-but-dead holder is a stale poison-pin → clear it + allow.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

from swarph_cli.capture import paths


def write_manifest(
    role: str,
    *,
    recipe: str,
    pin: str,
    service: str,
    lineage: str,
    session_id: Optional[str],
    live_pin_holder: Optional[str] = None,
) -> None:
    data = {
        "cell": role,
        "recipe": recipe,
        "pin": pin,
        "service": service,
        "lineage": lineage,
        "head": {
            "session_id": session_id,
            "jsonl_offset": None,
            "sha256": None,
            "last_compact_summary_offset": None,
            "live_pin_holder": live_pin_holder,
        },
    }
    _atomic_write_json(role, data)


def read_manifest(role: str) -> Optional[dict]:
    path = paths.manifest_path(role)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clear_live_pin(role: str) -> None:
    """Clear a stale live_pin_holder. No-op if the manifest is absent."""
    m = read_manifest(role)
    if m is None:
        return
    m.setdefault("head", {})["live_pin_holder"] = None
    _atomic_write_json(role, m)


def _atomic_write_json(role: str, data: dict) -> None:
    target = paths.manifest_path(role)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2, sort_keys=True)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
