"""``swarph postcompact-hook-output`` — PostCompact recall from the TIMELINE.

Called BY the PostCompact hook. Injects the last 7 days of the shared timeline
as ``additionalContext`` for the first post-compact turn.

WHY THE TIMELINE AND NOT GBRAIN (the obligation's whole point, 2026-08-21):
gbrain reindexes daily, so it structurally cannot contain TODAY — and the first
post-compact turn is exactly when today's highlights matter, because compaction
just evicted them from the context. The timeline is append-only and written at
the moment things happen, so it contains today by construction. A recall source
that cannot contain the current day is the wrong source for the turn that just
lost the current day.

Reads the local TIMELINE.md (``SWARPH_TIMELINE`` override; the canonical copy
on the gateway host, a pulled clone elsewhere) after a best-effort
``git pull --ff-only`` with a hard 2s timeout. Deterministic, stdlib-only, no
model, no network dependency — the pull is a freshener, never a precondition.

Failure-mode invariant (same as ``swarph hook-output`` / ``wake-hook-output``):
exit 0 on EVERY path, emit ``{}`` when there is nothing to say. The hook MUST
NOT block or crash the session — worst case is no recall, never a refused turn.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from swarph_cli.commands import timeline as tl

_WINDOW = dt.timedelta(days=7)
_MAX_CHARS = 6000
_PULL_TIMEOUT_S = 2.0


def _maybe_pull(timeline_path: str) -> None:
    """Best-effort ``git pull --ff-only`` on the timeline repo. Freshens the read
    on cells whose copy is a clone; a no-op-ish on the gateway host. EVERY failure
    is swallowed — a stale timeline is still a timeline."""
    repo = Path(timeline_path).parent
    try:
        if not (repo / ".git").exists():
            return
        subprocess.run(
            ["git", "-C", str(repo), "pull", "--ff-only", "-q"],
            capture_output=True, timeout=_PULL_TIMEOUT_S,
        )
    except Exception:
        pass


def _format(entries: list, dropped: int) -> str:
    lines = [
        f"POST-COMPACT RECALL — the shared timeline, last 7 days "
        f"({len(entries)} entries). gbrain reindexes daily and cannot contain "
        f"today; this can. Read these before re-deriving anything they already "
        f"settle.",
    ]
    if dropped:
        lines.append(f"({dropped} earlier entries omitted — the window is 7 "
                     f"days; the full file is TIMELINE.md.)")
    for e in entries:
        lines.append(f"- {e.ts.strftime('%Y-%m-%dT%H:%MZ')} · {e.cell} · {e.text}")
    return "\n".join(lines)


def run_postcompact_hook_output(argv: list[str] | None = None) -> int:
    # Drain the hook-input JSON; nothing in it is needed, but the protocol
    # expects stdin consumed without TTY-detection edge cases.
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass

    try:
        path = tl._timeline_path()
        _maybe_pull(path)
        entries = tl.load_entries(path)
    except Exception:
        print(json.dumps({}))
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - _WINDOW
    recent = [e for e in entries if cutoff <= e.ts <= now]
    if not recent:
        print(json.dumps({}))
        return 0

    dropped = 0
    text = _format(recent, dropped)
    while len(text) > _MAX_CHARS and recent:
        recent.pop(0)
        dropped += 1
        text = _format(recent, dropped)

    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PostCompact",
            "additionalContext": text,
        }
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_postcompact_hook_output(sys.argv[1:]))
