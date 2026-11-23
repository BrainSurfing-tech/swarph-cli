"""Card #1445 phase 1b — Codex rollout reader, writing ``openai.json``.

Source (design post 103202): the newest ``~/.codex/sessions/**/rollout-*.jsonl``
BY MTIME — a resumed session keeps its old dated name, so picking by name
reads a stale file. From that file take the LAST event carrying rate limits
(``payload.rate_limits`` or ``payload.info.rate_limits``) and map each window
by its ``window_minutes`` (10080 -> ``week``, 300 -> ``5h``).

``read_at`` is that event's own timestamp, so a fallback to an older
rollout is shown at its real age (STALE past the threshold) rather than
stamped with the sync clock. When the event has no timestamp, ``read_at``
is the rollout file's mtime and the document's ``source`` says so.

Writes NOTHING (no file, no partial) when no rollout anywhere carries a
mappable rate_limits event. A newest file whose rate_limits cannot be
mapped (used_percent present but resets_at absent, so the windows dict is
empty) falls through to the next older rollout that CAN be mapped, the
same way an empty file does. The write is atomic (temp file + rename).
Only the rate_limits figures are copied into openai.json: any other
payload keys (session text, credentials, tool I/O) are never read or written.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROVIDER = "openai"
SOURCE = "codex rollout JSONL token_count.rate_limits"
# Used only when the rate_limits event itself has no timestamp. The table
# then ages the reading by the file mtime, and source records that fact.
SOURCE_FROM_FILE_MTIME = (
    SOURCE
    + " (read_at is the rollout file mtime; the rate_limits event had no timestamp)"
)
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


def rollouts_newest_first(sessions_dir: "str | Path") -> list:
    """All rollout-*.jsonl, newest BY MTIME first (name breaks ties)."""
    root = Path(sessions_dir)
    if not root.is_dir():
        return []
    candidates = [p for p in root.rglob("rollout-*.jsonl") if p.is_file()]
    candidates.sort(key=lambda p: (p.stat().st_mtime, str(p)), reverse=True)
    return candidates


def newest_rollout(sessions_dir: "str | Path") -> "Path | None":
    """Newest rollout-*.jsonl BY MTIME, or None. Never by name."""
    ordered = rollouts_newest_first(sessions_dir)
    return ordered[0] if ordered else None


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


def last_rate_limits(rollout_path: "str | Path") -> "tuple | None":
    """The LAST event carrying rate_limits, as ``(limits, timestamp)``.

    ``timestamp`` is the event's own top-level ``timestamp`` field (or
    None when that field is absent). It is kept so openai.json can age
    the reading by the event, not by the moment the sync happened to run.
    """
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
                    stamp = record.get("timestamp") if isinstance(record, dict) else None
                    found = (limits, stamp)
    except OSError:
        return None
    return found


def _read_moment(rollout_path, event_timestamp):
    """``(datetime, source)`` for one rollout, or None if mtime is unreadable.

    The event timestamp wins. When the event has no timestamp (missing,
    blank, or unparseable), the rollout file's mtime is the reading time
    and ``source`` says so. Never the sync clock.
    """
    from swarph_cli.usage import parse_time

    parsed = parse_time(event_timestamp)
    if parsed is not None:
        return parsed, SOURCE
    try:
        mtime = Path(rollout_path).stat().st_mtime
        moment = datetime.fromtimestamp(mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return moment, SOURCE_FROM_FILE_MTIME


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


def build_document(rate_limits: dict, *, read_at: datetime,
                   source: "str | None" = None) -> dict:
    """The openai.json body. Empty windows dict when nothing mappable.

    ``read_at`` is the rate_limits event's own timestamp, or the rollout
    file's mtime when that event has none. It is not the sync clock: a
    fallback to an older rollout must show that reading's real age.
    """
    return {
        "provider": PROVIDER,
        "source": source or SOURCE,
        "read_at": _iso_z(read_at),
        "windows": windows_from_rate_limits(rate_limits),
    }


def write_openai_json(*, sessions_dir=None, usage_dir=None,
                       read_at=None) -> "dict | None":
    """Read the newest mappable rollout and atomically write openai.json.

    Candidates are walked newest-first by mtime. A rollout with no
    rate_limits event, and a rollout whose rate_limits cannot be mapped
    (windows empty — for example used_percent present but resets_at
    absent), both fall through to the next older rollout that CAN be
    mapped. A resumed session keeps its old dated name, and a fresh
    session's first events carry no usage figures.

    ``read_at`` in the written file is the chosen event's own timestamp.
    When that event has no timestamp it is the rollout file's mtime, and
    ``source`` says so. The optional ``read_at`` argument overrides that
    clock for a caller that already resolved one; the default is never
    ``datetime.now``.

    Returns {"path", "windows", "rollout"} on a write, None when nothing
    was written (no file anywhere, or no mappable rate_limits event in
    any rollout — and then no file or partial is created). Never raises
    on I/O: a reader that crashes the table is worse than a missing pool.
    """
    from swarph_cli.usage import default_usage_dir

    rollout = None
    document = None
    for candidate in rollouts_newest_first(codex_sessions_dir(sessions_dir)):
        found = last_rate_limits(candidate)
        if found is None:
            continue
        limits, event_ts = found
        if read_at is not None:
            moment, source = read_at, SOURCE
        else:
            resolved = _read_moment(candidate, event_ts)
            if resolved is None:
                continue
            moment, source = resolved
        draft = build_document(limits, read_at=moment, source=source)
        if not draft["windows"]:
            # Present but unmappable. Same as an empty file: keep walking.
            continue
        rollout, document = candidate, draft
        break
    if rollout is None or document is None:
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
