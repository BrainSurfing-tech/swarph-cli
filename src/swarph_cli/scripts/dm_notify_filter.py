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
import os
import queue
import sys
import threading
import time
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


def run_once(path: str, stdout: IO[str], *, timeout: float = 2.0) -> int:
    """Follow the inbox file from EOF, like tail -n 0. Print the first real DM.

    Bytes already in the file are not a wake. No child tail. Receipts are dropped.
    """
    deadline = time.monotonic() + timeout
    pos = None
    while time.monotonic() < deadline:
        try:
            with open(path, encoding="utf-8") as fh:
                if pos is None:
                    fh.seek(0, os.SEEK_END)
                    pos = fh.tell()
                else:
                    fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
        except FileNotFoundError:
            chunk = ""
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                d = d.get("dm") or d
                rendered = _format_dm(d) if isinstance(d, dict) else None
                if rendered is not None:
                    print(rendered, file=stdout, flush=True)
                    return 0
        time.sleep(0.05)
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dm_notify_filter")
    p.add_argument(
        "--idle-seconds",
        type=int,
        default=_IDLE_DEFAULT_SECONDS,
        help="silence-alert threshold (default: %(default)s)",
    )
    p.add_argument("--once", action="store_true",
                   help="follow an inbox file from EOF, print the first real DM, exit 0")
    p.add_argument("--inbox", default="", help="inbox.log path for --once")
    p.add_argument("--timeout", type=float, default=2.0,
                   help="seconds --once waits for a new DM (default: %(default)s)")
    args = p.parse_args(argv)
    if args.once:
        if not args.inbox:
            print("dm_notify_filter --once needs --inbox", file=sys.stderr)
            return 2
        return run_once(args.inbox, sys.stdout, timeout=args.timeout)
    return run_filter(sys.stdin, sys.stdout, idle_seconds=args.idle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
