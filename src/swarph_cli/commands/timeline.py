"""``swarph timeline`` — DETERMINISTIC temporal lookup over the git-backed timeline.

The TEMPORAL on-ramp of the OKF traversal brain (sub-project A). Reads the raw
``~/swarph-timeline/TIMELINE.md`` (the append-only, git-merged shared log) and
answers date-scoped questions — ``range``/``around``/``since`` — with NO model,
NO network, NO server ($0, deterministic). Each entry is an OKF *temporal node*:
its canonical id is its ISO timestamp; its edges are the ``[[links]]`` it names
(into the knowledge hemisphere). Complements the semantic ``brain-ask`` and the
structural ``codegraph``/``memory``.

Filters by each entry's EMBEDDED ``@ <ISO-timestamp>`` (the swarph-highlight line
format), never the git commit date — the entry's own date is canonical. Read-only;
stdlib-only; fail-safe (a missing/unreadable file → stderr note + non-zero, never
a traceback).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import namedtuple

from swarph_cli.commands.okf_links import parse_okf_links

Entry = namedtuple("Entry", "ts cell text links")

_DEFAULT_TIMELINE = os.path.expanduser("~/swarph-timeline/TIMELINE.md")
# - <ISO-ts> · **<cell>** · <rest>
_LINE = re.compile(r"^- (?P<ts>\S+)\s+·\s+\*\*(?P<cell>[^*]+)\*\*\s+·\s+(?P<rest>.*)$")


def _timeline_path() -> str:
    return os.environ.get("SWARPH_TIMELINE", _DEFAULT_TIMELINE)


def _parse_entry_ts(s: str) -> dt.datetime | None:
    """Parse the entry timestamp ``2026-07-15T08:51Z`` (minute precision, UTC)."""
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def load_entries(path: str) -> list:
    """Parse TIMELINE.md into Entry tuples. Raises OSError if unreadable (caller
    is fail-safe). Malformed lines (no match / bad ts) are skipped, not fatal."""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _LINE.match(line.rstrip("\n"))
            if not m:
                continue
            ts = _parse_entry_ts(m.group("ts"))
            if ts is None:
                continue
            rest = m.group("rest")
            entries.append(Entry(ts=ts, cell=m.group("cell").strip(),
                                 text=rest, links=parse_okf_links(rest)))
    return entries


def _parse_arg_date(s: str) -> dt.datetime:
    """Parse a CLI date arg. Accepts ``YYYY-MM-DD`` or a full ISO timestamp."""
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable date {s!r} (use YYYY-MM-DD)")


def _fmt_human(e: Entry) -> str:
    ts = e.ts.strftime("%Y-%m-%dT%H:%MZ")
    links = ("  " + " ".join(f"[[{l}]]" for l in e.links)) if e.links else ""
    return f"{ts} · {e.cell} · {e.text}{links}"


def run_timeline(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="swarph timeline",
        description="Deterministic temporal lookup over the git-backed swarph timeline "
                    "(range/around/since). $0, no model, no network.")
    sub = p.add_subparsers(dest="subcommand")
    pr = sub.add_parser("range", help="entries between two dates (inclusive)")
    pr.add_argument("start"); pr.add_argument("end")
    pa = sub.add_parser("around", help="entries within a window of a date")
    pa.add_argument("date"); pa.add_argument("--window", default="3d", help="e.g. 3d, 12h")
    ps = sub.add_parser("since", help="entries on/after a date")
    ps.add_argument("date")
    for sp in (pr, pa, ps):
        sp.add_argument("--json", action="store_true", help="OKF node/edge JSON")
    args = p.parse_args(argv)
    if not args.subcommand:
        p.print_help(); return 0

    try:
        entries = load_entries(_timeline_path())
    except OSError as e:
        print(f"swarph timeline: cannot read {_timeline_path()} ({e})", file=sys.stderr)
        return 1

    try:
        lo, hi = _bounds(args)
    except ValueError as e:
        print(f"swarph timeline: {e}", file=sys.stderr)
        return 1
    hits = [e for e in entries if (lo is None or e.ts >= lo) and (hi is None or e.ts <= hi)]

    if getattr(args, "json", False):
        print(json.dumps([_as_okf(e) for e in hits], indent=2))
    else:
        for e in hits:
            print(_fmt_human(e))
    return 0


def _parse_window(w: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)([dh])", w.strip())
    if not m:
        raise ValueError(f"bad --window {w!r} (use e.g. 3d or 12h)")
    n = int(m.group(1))
    return dt.timedelta(days=n) if m.group(2) == "d" else dt.timedelta(hours=n)


def _bounds(args):
    """(lo, hi) datetime bounds for the chosen subcommand; end-of-day for bare dates."""
    eod = dt.timedelta(hours=23, minutes=59)
    if args.subcommand == "range":
        return _parse_arg_date(args.start), _parse_arg_date(args.end) + eod
    if args.subcommand == "since":
        return _parse_arg_date(args.date), None
    if args.subcommand == "around":
        c = _parse_arg_date(args.date); w = _parse_window(args.window)
        return c - w, c + w + eod
    return None, None


def _as_okf(e: Entry) -> dict:
    ts = e.ts.strftime("%Y-%m-%dT%H:%MZ")
    return {
        "node": {"id": ts, "hemisphere": "time", "ts": ts},
        "edges": [{"type": "link", "to": l, "to_hemisphere": "knowledge",
                   "direction": "out"} for l in e.links],
        "cell": e.cell, "text": e.text,
    }
