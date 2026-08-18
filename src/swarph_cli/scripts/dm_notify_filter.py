"""Compact one-line-per-DM filter for harness background-watch pipelines.

Board card #482 (silent-wake hook bundle). Reference implementation:
workstation-lc's dm_notify_filter.py, verified live on Windows
2026-08-18 (DM 24479).

Pipeline shape:

    tail -n 0 -F <state_dir>/inbox.log | python -u -m swarph_cli.scripts.dm_notify_filter

Reads the swarph monitor's append-only inbox.log JSON-lines on stdin,
prints one short line per real DM. Non-DM lines (receipts, monitor
chatter) are dropped so notifications stay signal.

Portability constraint (workstation-lc, measured on metal): select() on
Windows supports SOCKETS ONLY — calling it on a pipe/stdin raises OSError
with no degradation. So the blocking read lives on a daemon reader thread
and the main thread uses queue.get(timeout=...), which distinguishes
"no line yet" from "no line ever again" (EOF) without platform-specific
calls. Do NOT "simplify" this back to select().

-u / flush=True is load-bearing: a buffered stage makes the watch silent
while looking armed, which is the exact failure this watch exists for.

Silence alert: after --idle-seconds with no DM activity, prints one
[MESH WATCH] silence line and re-arms (fires once per quiet period, no
spam). EOF on stdin prints a distinct deaf-watch line and exits nonzero —
a tail that lost its file is a watcher that will never fire again.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from typing import IO, Any, Optional

_IDLE_DEFAULT_SECONDS = 1800


def _reader(stream: IO[str], q: "queue.Queue[Optional[str]]") -> None:
    """Daemon thread: blocking readline loop, None sentinel on EOF."""
    try:
        for line in stream:
            q.put(line)
    except Exception:
        pass
    finally:
        q.put(None)


def _format_dm(d: dict[str, Any]) -> Optional[str]:
    mid, frm, kind = d.get("id"), d.get("from_node"), d.get("kind")
    if not (mid and frm):
        return None
    body = (d.get("content") or "").replace("\n", " ")[:110]
    if body.startswith("receipt:"):
        return None
    return f"[MESH DM] id={mid} from={frm} kind={kind} | {body}"


def run_filter(
    stdin: IO[str],
    stdout: IO[str],
    *,
    idle_seconds: int = _IDLE_DEFAULT_SECONDS,
    max_lines: Optional[int] = None,
) -> int:
    """Filter loop. ``max_lines`` bounds processed lines (test hook)."""
    q: "queue.Queue[Optional[str]]" = queue.Queue()
    threading.Thread(target=_reader, args=(stdin, q), daemon=True).start()

    quiet = False
    processed = 0
    while True:
        try:
            line = q.get(timeout=idle_seconds)
        except queue.Empty:
            if not quiet:
                print(
                    f"[MESH WATCH] no DM for {idle_seconds}s — "
                    "if you expect traffic, check `swarph monitor status`",
                    file=stdout,
                    flush=True,
                )
                quiet = True
            continue
        if line is None:
            print(
                "[MESH WATCH] inbox stream EOF — this watch is DEAF. "
                "Re-arm it or DMs will arrive unnoticed.",
                file=stdout,
                flush=True,
            )
            return 1

        line = line.strip()
        if line:
            try:
                d = json.loads(line)
            except Exception:
                d = None
            if isinstance(d, dict):
                d = d.get("dm") or d
                rendered = _format_dm(d)
                if rendered is not None:
                    print(rendered, file=stdout, flush=True)
                    quiet = False
            processed += 1
            if max_lines is not None and processed >= max_lines:
                return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dm_notify_filter")
    p.add_argument(
        "--idle-seconds",
        type=int,
        default=_IDLE_DEFAULT_SECONDS,
        help="silence-alert threshold (default: %(default)s)",
    )
    args = p.parse_args(argv)
    return run_filter(sys.stdin, sys.stdout, idle_seconds=args.idle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
