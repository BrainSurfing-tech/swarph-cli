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
import re
import sys
import threading
import time
from typing import IO, Any, Optional

_IDLE_DEFAULT_SECONDS = 1800

# Card #1066 (quiet wakes): TAKEN receipts and repo.merged events are logged
# but must not wake on their own — held for the next real DM or a cap.
# Per-cell opt-in, default off; unset behaviour is byte-identical to today.
_QUIET_ENV = "SWARPH_QUIET_WAKES"
_QUIET_CAP_S = 6 * 3600

_TAKEN_RE = re.compile(r"OBLIGATION #\d+ TAKEN by ")

# Card #1050: ANY per-line failure is a logged skip, never a process death
# (a death in --once mode reads as "no DM arrived"). The notice keeps the
# [MESH DM prefix and goes to stdout so a downstream waker wakes on it;
# it is ASCII-only so it cannot itself raise on a hostile-errors stdout.
_SKIP_NOTICE = "[MESH DM] unrenderable line skipped"


def _reader(stream: IO[str], q: "queue.Queue[Optional[str]]") -> None:
    """Daemon thread: blocking readline loop, None sentinel on EOF."""
    try:
        for line in stream:
            q.put(line)
    except Exception:
        pass
    finally:
        q.put(None)


def is_real_dm(d: dict[str, Any]) -> bool:
    """True when the line is a DM the wake should surface. Receipts are not."""
    return _format_dm(d) is not None


def is_quiet_dm(d: dict[str, Any]) -> bool:
    """Card #1066: True when the DM is logged but must not wake on its own —
    (a) an obligation TAKEN receipt (kind answer + TAKEN verb; CLOSED and
    questions stay real) or (b) a repo.merged swarph_event (kind fyi + JSON
    content naming the event; plain-text fyi stays real). Anything
    unparseable is NOT quiet: an unevaluable DM must wake, never hide."""
    if not isinstance(d, dict):
        return False
    kind = d.get("kind")
    content = d.get("content")
    if not isinstance(content, str):
        return False
    if kind == "answer" and _TAKEN_RE.search(content):
        return True
    if kind == "fyi":
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return False
        if isinstance(payload, dict):
            ev = payload.get("swarph_event")
            if isinstance(ev, dict) and ev.get("event") == "repo.merged":
                return True
    return False


def quiet_enabled(explicit: Optional[bool] = None) -> bool:
    """Per-cell opt-in, default off. An explicit argument (tests, callers
    with their own flag) beats the env var."""
    if explicit is not None:
        return explicit
    return os.environ.get(_QUIET_ENV, "").strip().lower() in (
        "1", "true", "yes")


def _flush_due(held: list, now: float, cap_s: float, stdout: IO[str]) -> list:
    """Print held lines whose cap passed (oldest first), return the rest.
    BrokenPipeError propagates — a closed pipe stays loud (SIGPIPE contract)."""
    due = [h for h in held if now - h[0] >= cap_s]
    for _, rendered in due:
        print(rendered, file=stdout, flush=True)
    if due:
        kept = [h for h in held if now - h[0] < cap_s]
        return kept
    return held


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
    quiet: Optional[bool] = None,
    quiet_cap_s: float = _QUIET_CAP_S,
    now: Optional[Any] = None,
) -> int:
    """Filter loop. ``max_lines`` bounds processed lines (test hook).

    Card #1066: when quiet wakes are enabled (explicit flag or
    SWARPH_QUIET_WAKES), TAKEN receipts and repo.merged events are HELD —
    printed with the next real DM (held first, they arrived earlier) or
    once each passes the cap, or on EOF so a dying watch loses nothing.
    ``now`` is a clock hook (tests); ``quiet`` None reads the env.
    """
    clock = now if now is not None else time.time
    quiet_on = quiet_enabled(quiet)
    q: "queue.Queue[Optional[str]]" = queue.Queue()
    threading.Thread(target=_reader, args=(stdin, q), daemon=True).start()

    silent = False
    processed = 0
    held: list = []
    while True:
        # Bound the wait by the next cap deadline so a lone quiet DM
        # flushes on time with no new traffic.
        timeout = idle_seconds
        if quiet_on and held:
            timeout = max(0.0, min(
                idle_seconds,
                min(t for t, _ in held) + quiet_cap_s - clock()))
        try:
            line = q.get(timeout=timeout)
        except queue.Empty:
            if quiet_on:
                before = len(held)
                held = _flush_due(held, clock(), quiet_cap_s, stdout)
                if len(held) != before:
                    silent = False
                    continue
            if not silent:
                print(
                    f"[MESH WATCH] no DM for {idle_seconds}s — "
                    "if you expect traffic, check `swarph monitor status`",
                    file=stdout,
                    flush=True,
                )
                silent = True
            continue
        if line is None:
            for _, rendered in held:
                print(rendered, file=stdout, flush=True)
            held = []
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
                try:
                    d = d.get("dm") or d
                    rendered = _format_dm(d)
                    if rendered is not None:
                        if quiet_on and is_quiet_dm(d):
                            held.append((clock(), rendered))
                        else:
                            for _, h in held:
                                print(h, file=stdout, flush=True)
                            held = []
                            print(rendered, file=stdout, flush=True)
                            silent = False
                except BrokenPipeError:
                    raise  # a closed pipe stays loud (SIGPIPE contract)
                except Exception:
                    print(_SKIP_NOTICE, file=stdout, flush=True)
            processed += 1
            if max_lines is not None and processed >= max_lines:
                return 0


def run_once(path: str, stdout: IO[str], *, timeout: float = 2.0,
             quiet: Optional[bool] = None) -> int:
    """Follow the inbox file from EOF, like tail -n 0. Print the first real DM.

    Bytes already in the file are not a wake. No child tail. Receipts are dropped.

    Card #1066: with quiet wakes enabled, quiet lines are HELD, not printed —
    the first real DM prints the backlog plus itself. A window with only
    quiet lines returns 1 without printing (they persist in inbox.log for
    the streaming filter, which owns cap delivery).
    """
    quiet_on = quiet_enabled(quiet)
    deadline = time.monotonic() + timeout
    pos = None
    held: list = []
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
                try:
                    d = d.get("dm") or d
                    rendered = _format_dm(d) if isinstance(d, dict) else None
                    if rendered is not None:
                        if quiet_on and is_quiet_dm(d):
                            held.append(rendered)
                            continue
                        for h in held:
                            print(h, file=stdout, flush=True)
                        print(rendered, file=stdout, flush=True)
                        return 0
                except BrokenPipeError:
                    raise  # a closed pipe stays loud (SIGPIPE contract)
                except Exception:
                    print(_SKIP_NOTICE, file=stdout, flush=True)
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
    try:
        # Card #1050: lone surrogates in DM content must render (as ?) and
        # continue, never kill the watch on a strict-errors stdout. Set
        # before EITHER print path: --once and filter mode both emit DMs.
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if args.once:
        if not args.inbox:
            print("dm_notify_filter --once needs --inbox", file=sys.stderr)
            return 2
        return run_once(args.inbox, sys.stdout, timeout=args.timeout)
    return run_filter(sys.stdin, sys.stdout, idle_seconds=args.idle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
