"""``swarph timeline`` — DETERMINISTIC temporal lookup over the git-backed timeline.

The TEMPORAL on-ramp of the OKF traversal brain (sub-project A). Reads the raw
``~/swarph-timeline/TIMELINE.md`` (the append-only, git-merged shared log) and
answers date-scoped questions — ``range``/``around``/``since`` — with NO model,
NO network, NO server ($0, deterministic). Each entry is an OKF *temporal node*:
its canonical id is its ISO timestamp; its edges are the ``[[links]]`` it names
(into the knowledge hemisphere). Complements the semantic ``brain-ask`` and the
structural ``codegraph``/``memory``.

Filters by each entry's EMBEDDED ISO timestamp (the line format: ``- <ISO-ts> · **<cell>** · <text>``),
never the git commit date — the entry's own date is canonical. Read-only;
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


# EVERY FORM THE SHARED TIMELINE ACTUALLY CONTAINS, not only the one we write today.
#
# MEASURED on the live TIMELINE.md 2026-07-27, by shape:
#     274  NNNN-NN-NNTNN:NNZ      minute precision — the only form this parsed
#      65  NNNN-NN-NN             DATE ONLY — the OMEGA genesis entries
#       1  NNNN-NN-NNTNN:NN:NNZ   seconds
# The strict minute-precision parser silently dropped 66 of 340 entries — THE MESH'S
# ENTIRE PRE-HISTORY, 2026-03 to 2026-04: genesis, the first worker swarm, the storage
# hub, OMEGA Command, Knowledge Graph v1.0. `swarph timeline since 2026-03-01` returned
# NOTHING and exited 0 — this tool's own headline defect (card #135) committed against
# the oldest and least replaceable content it holds.
#
# Found because the gateway's GET /highlights reported parse_skipped=67 on its first
# live call — a counter that exists only because a peer said the parser should not be
# exempt from the endpoint's own empty-is-not-blind rule.
#
# A DATE-ONLY ENTRY IS ANCHORED AT 00:00Z. That is a choice, not a rounding: such an
# entry records a DAY, so it sorts at the start of that day and `since <that day>`
# includes it.
_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",   # sub-second
    "%Y-%m-%dT%H:%M:%SZ",      # seconds
    "%Y-%m-%dT%H:%MZ",         # minute — what `swarph highlight` writes
    "%Y-%m-%d %H:%M:%SZ",      # space separator, hand-written entries
    "%Y-%m-%d %H:%MZ",
    "%Y-%m-%d",                # DATE ONLY — the genesis entries
)


def _parse_entry_ts(s: str) -> dt.datetime | None:
    """Parse an entry timestamp in any form the timeline actually uses. UTC.

    Returns None for anything unrecognised — the caller REPORTS it as skipped, never
    guesses. A guessed timestamp files an entry under the wrong day, which is worse
    than admitting the line was not understood.
    """
    for fmt in _TS_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    # Offset forms (+00:00, -05:00) and other ISO-8601 shapes. The explicit formats
    # run first so the common case takes no exception path. `Z` is spelled out because
    # fromisoformat did not accept it before 3.11 and cells run 3.10.
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


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


def _is_bare_date(s: str) -> bool:
    """True if the CLI date arg is a bare ``YYYY-MM-DD`` (no time component).

    A bare date has no ``T``/time part; a full ISO timestamp (``%Y-%m-%dT%H:%MZ``)
    does. Used by ``_bounds`` to decide whether the end-of-day nudge applies —
    it must NOT apply to an explicit timestamp, or the caller's exact bound
    silently shifts by ~24h.
    """
    return "T" not in s


def _fmt_human(e: Entry) -> str:
    ts = e.ts.strftime("%Y-%m-%dT%H:%MZ")
    return f"{ts} · {e.cell} · {e.text}"


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
    """(lo, hi) datetime bounds for the chosen subcommand; end-of-day for bare dates.

    The end-of-day nudge only applies when the relevant arg is a bare
    ``YYYY-MM-DD`` — a full ISO timestamp is an exact bound and must come
    back unchanged (see ``_is_bare_date``).
    """
    eod = dt.timedelta(hours=23, minutes=59)
    if args.subcommand == "range":
        hi = _parse_arg_date(args.end)
        if _is_bare_date(args.end):
            hi += eod
        return _parse_arg_date(args.start), hi
    if args.subcommand == "since":
        return _parse_arg_date(args.date), None
    if args.subcommand == "around":
        c = _parse_arg_date(args.date); w = _parse_window(args.window)
        hi = c + w
        if _is_bare_date(args.date):
            hi += eod
        return c - w, hi
    return None, None


def _as_okf(e: Entry) -> dict:
    ts = e.ts.strftime("%Y-%m-%dT%H:%MZ")
    return {
        "node": {"id": ts, "hemisphere": "time", "ts": ts},
        "edges": [{"type": "link", "to": l, "to_hemisphere": "knowledge",
                   "direction": "out"} for l in e.links],
        "cell": e.cell, "text": e.text,
    }
