"""Card #1445 phase 1b — Codex rollout reader, writing ``openai.json``.

Source (design post 103202): the newest ``~/.codex/sessions/**/rollout-*.jsonl``
BY MTIME — a resumed session keeps its old dated name, so picking by name
reads a stale file. From that file take the LAST event carrying rate limits
(``payload.rate_limits`` or ``payload.info.rate_limits``) and map each window
by its ``window_minutes`` (10080 -> ``week``, 300 -> ``5h``).

Writes NOTHING (no file, no partial) when no event carries rate_limits.
The write is atomic (temp file + rename). No credential is read or printed
anywhere: the rollout log carries usage figures only.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROVIDER = "openai"
SOURCE = "codex rollout JSONL token_count.rate_limits"
FILENAME = "openai.json"

#: window_minutes -> window name in openai.json (same names as muse.json).
WINDOW_NAMES = {10080: "week", 300: "5h"}


def _iso_z(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resets_to_iso(value):
    """Epoch seconds -> UTC ISO Z (like muse.json). Strings pass through."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return _iso_z(datetime.fromtimestamp(value, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def codex_sessions_dir(explicit: "str | Path | None" = None) -> Path:
    """Where the rollout logs live. Tests pass an explicit tmp dir."""
    if explicit is not None:
        return Path(explicit)
    return Path.home() / ".codex" / "sessions"


def newest_rollout(sessions_dir: "str | Path") -> "Path | None":
    """Newest rollout-*.jsonl BY MTIME, or None. Never by name."""
    root = Path(sessions_dir)
    if not root.is_dir():
        return None
    candidates = [p for p in root.rglob("rollout-*.jsonl") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.stat().st_mtime, str(p)))


def _rate_limits_of(record) -> "dict | None":
    """The rate_limits payload of one JSONL event, or None."""
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    direct = payload.get("rate_limits")
    if isinstance(direct, dict):
        return direct
    info = payload.get("info")
    if isinstance(info, dict):
        nested = info.get("rate_limits")
        if isinstance(nested, dict):
            return nested
    return None


def last_rate_limits(rollout_path: "str | Path") -> "dict | None":
    """The LAST event in the file carrying rate_limits, or None."""
    found = None
    try:
        with open(rollout_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                limits = _rate_limits_of(record)
                if limits is not None:
                    found = limits
    except OSError:
        return None
    return found


def windows_from_rate_limits(rate_limits: dict) -> dict:
    """{window_name: {used_percentage, resets_at}} mapped by window_minutes."""
    windows = {}
    for entry in rate_limits.values():
        if not isinstance(entry, dict):
            continue
        minutes = entry.get("window_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
            continue
        minutes = int(minutes)
        name = WINDOW_NAMES.get(minutes, f"{minutes}m")
        used = entry.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        resets = _resets_to_iso(entry.get("resets_at"))
        if resets is None:
            continue
        windows[name] = {"used_percentage": float(used), "resets_at": resets}
    return windows


def build_document(rate_limits: dict, *, read_at: "datetime | None" = None) -> dict:
    """The openai.json body. Empty windows dict when nothing mappable."""
    moment = read_at or datetime.now(timezone.utc)
    return {
        "provider": PROVIDER,
        "source": SOURCE,
        "read_at": _iso_z(moment),
        "windows": windows_from_rate_limits(rate_limits),
    }


def write_openai_json(*, sessions_dir=None, usage_dir=None,
                       read_at=None) -> "dict | None":
    """Read the newest rollout and atomically write openai.json.

    Returns {"path", "windows", "rollout"} on a write, None when nothing
    was written (no file, or no event carrying rate_limits — and then no
    file or partial is created). Never raises on I/O: a reader that
    crashes the table is worse than a missing pool.
    """
    from swarph_cli.usage import default_usage_dir

    rollout = newest_rollout(codex_sessions_dir(sessions_dir))
    if rollout is None:
        return None
    limits = last_rate_limits(rollout)
    if limits is None:
        return None
    document = build_document(limits, read_at=read_at)
    if not document["windows"]:
        return None
    target_dir = Path(usage_dir) if usage_dir is not None else default_usage_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / FILENAME
        fd, tmp_name = tempfile.mkstemp(dir=str(target_dir),
                                        prefix=FILENAME + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return None
    return {"path": str(target), "windows": document["windows"],
            "rollout": str(rollout)}
