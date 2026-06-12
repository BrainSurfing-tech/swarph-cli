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
from typing import List, Optional, Tuple

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


def set_live_pin(role: str, holder: str) -> None:
    """Record `holder` (a tmux session name) as the live resumer of this
    cell's pinned UUID. No-op if the manifest is absent — un-hardened cells
    are untracked by design (harden is the opt-in)."""
    m = read_manifest(role)
    if m is None:
        return
    m.setdefault("head", {})["live_pin_holder"] = holder
    _atomic_write_json(role, m)


def find_pin_holders(session_id: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Sweep captures/*.json for live holders of ``session_id``.

    Returns ``(holders, corrupt)`` where:
      * ``holders`` = ``(clear_key, display_holder)`` for every manifest whose
        head.session_id matches AND carries a non-null live_pin_holder.
        ``clear_key`` is the on-disk filename stem (a SAFE role for
        clear_live_pin); ``display_holder`` is the recorded tmux-session name
        (for the probe + the refuse message), NOT used to build any path.
      * ``corrupt`` = filenames that could not be parsed.

    The cross-NAME sweep behind the double-resume gate: the footgun is
    per-UUID, not per-role — two cell names pinning one UUID each show a clean
    own-manifest, so verify must scan EVERY manifest (renamed-cell incident,
    spec §4.3). FAIL-CLOSED on corruption: an unparseable manifest could be
    hiding a live holder of this UUID, so verify must REFUSE rather than
    silently pass (a silent fail-OPEN in the gate the spec exists to provide).
    """
    holders: List[Tuple[str, str]] = []
    corrupt: List[str] = []
    cdir = paths.captures_dir()
    if not cdir.exists():
        return holders, corrupt
    for path in sorted(cdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            corrupt.append(path.name)
            continue
        head = (data.get("head") or {}) if isinstance(data, dict) else {}
        holder = head.get("live_pin_holder")
        if head.get("session_id") == session_id and holder:
            # clear_key = the filename stem (validated when re-built into a
            # path), never the attacker-controllable "cell" field.
            holders.append((path.stem, holder))
    return holders, corrupt


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
