"""Card #1445 phase 1b — ``swarph usage``: one pools table + Codex sync.

``swarph usage`` reads ``<state-root>/usage/*.json`` (state root honours
``SWARPH_STATE_ROOT``, else ``~/swarph_state``) and prints one row per
pool+window: pool, used %, resets (UTC and in-hours), read age, burn per
hour when a previous distinct reading exists. ``--json`` emits the same
rows as JSON.

``swarph usage sync`` runs the Codex rollout reader, writing
``usage/openai.json`` (atomic; nothing written when no event exists),
and the opencode Go reader, writing ``usage/opencode.json`` from
``/var/lib/swarph-usage/opencode-go.json`` (override with
``--opencode-file`` or ``SWARPH_OPENCODE_USAGE_FILE``). The opencode
document's ``read_at`` is that file body's ``read_at``, not the file
mtime and not the sync clock. A window with no finite numeric percent
(missing, non-numeric, NaN, or Infinity) is omitted, never written as
a fresh zero.

Keep ``openai.json`` under the 6h STALE mark by running sync on a user
crontab or a user systemd timer. This package installs neither. Cron's
PATH is ``/usr/bin:/bin``, so both schedule forms call swarph by the
absolute path from ``command -v swarph``. On Lab-ovh that path is
``/home/ubuntu/.local/bin/swarph``. A reading is STALE only when its age
is greater than 6h, so a 4h cadence stays inside the mark:

    0 */4 * * * /home/ubuntu/.local/bin/swarph usage sync

The user timer equivalent is OnCalendar=*-*-* 00/4:00:00 with:

    ExecStart=/home/ubuntu/.local/bin/swarph usage sync

Burn has no history store today: the table keeps a last-seen snapshot in
the ``.usage-last-seen.json`` dotfile in the usage dir (excluded from the
pool glob by name) and reports burn/hr = (used_now - used_prev) /
read_at-delta-hours for the same pool+window when a previous DISTINCT
reading exists, else blank. A single reading never shows a fabricated
burn. Burn is never negative: a used% drop, a moved window resets_at, or
a changed reading source between snapshot and now blanks the burn and
marks the row RESET (text note, JSON ``reset`` flag). A source change
from null to a value counts as a reset. A used rise with an unchanged or
null resets_at keeps the burn and is not marked RESET. Limit that remains:
a rise right after a reset, with no resets_at to compare, cannot be told
apart from ordinary burn. The snapshot only advances on a strictly newer
reading, so re-showing the same reading carries the last burn forward
instead of blanking it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from swarph_cli.commands.mesh import (
    _add_common,
    _http_get_json,
    _resolve_self_name,
    _resolve_token,
)
from swarph_cli.usage import (
    SYNC_CRON_LINE,
    SYNC_TIMER_EXECSTART,
    collect_rows,
    default_usage_dir,
    load_pool_map,
    load_snapshot,
    render_table,
    rows_to_json,
    save_snapshot,
)
from swarph_cli.usage import timeline as _timeline
from swarph_cli.usage.codex import codex_sessions_dir, write_openai_json
from swarph_cli.usage.opencode import opencode_usage_path, write_opencode_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarph usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Keep openai.json under the 6h STALE mark with a user crontab\n"
            "or a user systemd timer. This package installs neither.\n"
            "Cron's PATH is /usr/bin:/bin, so use the absolute path from\n"
            "`command -v swarph` (on Lab-ovh, /home/ubuntu/.local/bin/swarph).\n"
            "A reading is STALE only when its age is greater than 6h, so\n"
            "a 4h cadence stays inside the mark:\n"
            f"    {SYNC_CRON_LINE}\n"
            "    # user timer: OnCalendar=*-*-* 00/4:00:00\n"
            f"    # {SYNC_TIMER_EXECSTART}\n"
        ),
        description="Usage pools table (card #1445): pool, used %, resets, "
                    "read age, burn/hr. A reading older than 6h is STALE. "
                    "A source change from null to a value counts as a reset. "
                    "A used rise with an unchanged or null resets_at keeps "
                    "the burn. Limit that remains: a rise right after a "
                    "reset, with no resets_at to compare, cannot be told "
                    "apart from ordinary burn. "
                    "Keep openai.json under the 6h STALE mark with a user "
                    f"crontab line `{SYNC_CRON_LINE}` or a user systemd "
                    "timer OnCalendar=*-*-* 00/4:00:00 whose "
                    f"`{SYNC_TIMER_EXECSTART}` uses the absolute path from "
                    "`command -v swarph`. This package installs neither. "
                    "Timeline definitions: rows sort by (created_at, id); "
                    "rework rk#N groups by (step, task) and counts taken "
                    "rows only (rk#- while untaken); held is taken->closed "
                    "where both timestamps exist (BAD-ORDER when reversed), "
                    "open-held/open-age apply to status open only and are "
                    "measured to --as-of (default now) and marked *; "
                    "commander-held means holder == 'commander'; a peer-day "
                    "row counts when the peer took it or closed it "
                    "(closed_by) that UTC day; an unknown peer is an error, "
                    "a quiet day is an empty success; --day is strict "
                    "YYYY-MM-DD; missing timestamps render '-' and never "
                    "crash.",
    )
    parser.add_argument("action", nargs="?", default="show",
                        choices=("show", "sync", "timeline"),
                        help="'show' prints the table (default); 'sync' runs "
                             "the Codex rollout reader into openai.json and "
                             "the opencode Go reader into opencode.json; "
                             "'timeline' reads board-obligation history "
                             "(--card N, or --peer P --day YYYY-MM-DD).")
    parser.add_argument("--json", action="store_true",
                        help="emit the same rows as JSON.")
    parser.add_argument("--card", type=int, default=None, metavar="N",
                        help="timeline: every obligation row on card N, oldest "
                             "first. Mutually exclusive with --peer/--day.")
    parser.add_argument("--peer", default=None, metavar="PEER",
                        help="timeline: rows PEER took or closed on --day "
                             "(UTC). Requires --day.")
    parser.add_argument("--day", default=None, metavar="YYYY-MM-DD",
                        help="timeline: the UTC calendar day for --peer. "
                             "A row counts when taken_at OR closed_at falls "
                             "in [day 00:00Z, next 00:00Z).")
    parser.add_argument("--as-of", default=None, metavar="TIMESTAMP",
                        help="timeline: the instant open-row ages are measured "
                             "to (default: now, UTC). ISO timestamps only.")
    _add_common(parser)
    parser.add_argument("--state-root", default=None,
                        help="state root holding usage/ "
                             "(default: $SWARPH_STATE_ROOT or ~/swarph_state).")
    parser.add_argument("--codex-sessions", default=None,
                        help="Codex sessions dir for 'sync' "
                             "(default: ~/.codex/sessions).")
    parser.add_argument("--opencode-file", default=None,
                        help="opencode Go usage file for 'sync' "
                             "(default: $SWARPH_OPENCODE_USAGE_FILE or "
                             "/var/lib/swarph-usage/opencode-go.json).")
    return parser


def _usage_dir(state_root) -> Path:
    if state_root:
        return Path(state_root).expanduser() / "usage"
    return default_usage_dir()


def _run_show(args) -> int:
    usage_dir = _usage_dir(args.state_root)
    try:
        pool_map = load_pool_map(usage_dir)
    except (OSError, ValueError) as exc:
        print(f"swarph usage: pool map unreadable: {exc}", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    snapshot = load_snapshot(usage_dir)
    rows = collect_rows(usage_dir=usage_dir, pool_map=pool_map,
                        now=now, snapshot=snapshot)
    save_snapshot(usage_dir, rows, snapshot)
    if args.json:
        print(json.dumps(rows_to_json(rows), indent=2))
    else:
        print(render_table(rows), end="")
    return 0


def _run_sync(args) -> int:
    usage_dir = _usage_dir(args.state_root)
    sessions = codex_sessions_dir(args.codex_sessions)
    result = write_openai_json(sessions_dir=sessions, usage_dir=usage_dir)
    if result is None:
        print("swarph usage sync: no rate_limits event in any "
              f"rollout under {sessions}; wrote nothing.")
    else:
        windows = ", ".join(
            f"{name} {entry['used_percentage']:.1f}%"
            for name, entry in sorted(result["windows"].items()))
        print(f"swarph usage sync: wrote {result['path']} ({windows}).")
    opencode_path = opencode_usage_path(args.opencode_file)
    opencode = write_opencode_json(usage_file=opencode_path,
                                   usage_dir=usage_dir)
    if opencode is None:
        print("swarph usage sync: no mappable opencode usage in "
              f"{opencode_path}; wrote nothing for opencode.")
    else:
        windows = ", ".join(
            f"{name} {entry['used_percentage']:.1f}%"
            for name, entry in sorted(opencode["windows"].items()))
        print(f"swarph usage sync: wrote {opencode['path']} ({windows}).")
    return 0


def _gateway_error(status, data):
    detail = data.get("detail", data) if isinstance(data, dict) else data
    print(f"swarph usage timeline: gateway {status or 'unreachable'}: "
          f"{detail}", file=sys.stderr)
    return 1


def _run_timeline(args) -> int:
    """Read-only board-obligation history. GETs only — no write helper is
    imported or called on this path (pinned by the no-write test)."""
    from swarph_cli.commands.board import _obligations_list_url

    if args.card is not None and (args.peer is not None or args.day is not None):
        print("swarph usage timeline: --card is mutually exclusive with "
              "--peer/--day", file=sys.stderr)
        return 2
    if args.card is None and (args.peer is None or args.day is None):
        print("swarph usage timeline: pass --card N or --peer P "
              "--day YYYY-MM-DD", file=sys.stderr)
        return 2
    if args.day is not None:
        try:
            day_start = _timeline.parse_day(args.day)
        except ValueError as exc:
            print(f"swarph usage timeline: {exc}", file=sys.stderr)
            return 2
    else:
        day_start = None
    if args.as_of is not None:
        as_of = _timeline.parse_ts(args.as_of)
        if as_of is None:
            print(f"swarph usage timeline: bad --as-of {args.as_of!r} "
                  f"(ISO timestamps only)", file=sys.stderr)
            return 2
    else:
        as_of = datetime.now(timezone.utc)
    try:
        self_name = _resolve_self_name(args.self_name)
        token = _resolve_token(self_name, args.token_file)
    except RuntimeError as exc:
        print(f"swarph usage timeline: {exc}", file=sys.stderr)
        return 1
    gw = args.gateway.rstrip("/")

    if args.card is not None:
        st, data = _http_get_json(
            _obligations_list_url(gw, card_id=args.card), token)
        if not (st and 200 <= st < 300) or not isinstance(data, dict):
            return _gateway_error(st, data)
        rows = [r for r in (data.get("obligations") or [])
                if isinstance(r, dict) and r.get("card_id") == args.card]
        cst, card = _http_get_json(f"{gw}/board/cards/{args.card}", token)
        if cst == 404:
            print(f"swarph usage timeline: no such card #{args.card}",
                  file=sys.stderr)
            return 1
        if not (cst and 200 <= cst < 300) or not isinstance(card, dict):
            return _gateway_error(cst, card)
        summary = _timeline.summarize(rows, as_of)
        if args.json:
            print(json.dumps(
                _timeline.card_to_json(args.card, rows, as_of, summary),
                indent=2))
        else:
            print(_timeline.render_card(
                args.card, card, rows, as_of, summary))
        return 0
    # Peer-day is ONE unfiltered GET of the same obligations list URL:
    # the endpoint has no closed_by filter (only status/holder/card_id/
    # overdue), and a row the peer closed may be held by someone else, so
    # a holder-filtered fetch would hide exactly the rows (D) adds. The
    # peer filter below is client-side.
    st, data = _http_get_json(_obligations_list_url(gw), token)
    if not (st and 200 <= st < 300) or not isinstance(data, dict):
        return _gateway_error(st, data)
    rows = [r for r in (data.get("obligations") or []) if isinstance(r, dict)]
    if not _timeline.peer_known(rows, args.peer):
        print(f"swarph usage timeline: unknown peer {args.peer!r} "
              f"(no rows held or closed by them)", file=sys.stderr)
        return 1
    entries = _timeline.peer_day_entries(rows, day_start, args.peer)
    if args.json:
        print(json.dumps(
            _timeline.peer_to_json(args.peer, day_start, entries, as_of),
            indent=2))
    else:
        print(_timeline.render_peer(args.peer, day_start, entries, as_of))
    return 0


def run_usage(argv) -> int:
    """Entry point registered as the ``usage`` verb in main._VERB_HANDLERS."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.action == "sync":
        return _run_sync(args)
    if args.action == "timeline":
        return _run_timeline(args)
    return _run_show(args)
