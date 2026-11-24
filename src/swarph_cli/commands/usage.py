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
mtime and not the sync clock. A window with no numeric percent is
omitted, never written as a fresh zero.

Keep ``openai.json`` under the 6h STALE mark by running sync on a user
crontab or a user systemd timer. This package installs neither. A
reading is STALE only when its age is greater than 6h, so a 4h cadence
stays inside the mark:

    0 */4 * * * swarph usage sync

The user timer equivalent is OnCalendar=*-*-* 00/4:00:00 with
ExecStart=swarph usage sync.

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

from swarph_cli.usage import (
    SYNC_CRON_LINE,
    collect_rows,
    default_usage_dir,
    load_pool_map,
    load_snapshot,
    render_table,
    rows_to_json,
    save_snapshot,
)
from swarph_cli.usage.codex import codex_sessions_dir, write_openai_json
from swarph_cli.usage.opencode import opencode_usage_path, write_opencode_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarph usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Keep openai.json under the 6h STALE mark with a user crontab\n"
            "or a user systemd timer. This package installs neither.\n"
            "A reading is STALE only when its age is greater than 6h, so\n"
            "a 4h cadence stays inside the mark:\n"
            f"    {SYNC_CRON_LINE}\n"
            "    # user timer: OnCalendar=*-*-* 00/4:00:00\n"
            "    # ExecStart=swarph usage sync\n"
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
                    "timer OnCalendar=*-*-* 00/4:00:00 whose ExecStart is "
                    "`swarph usage sync`. This package installs neither.",
    )
    parser.add_argument("action", nargs="?", default="show",
                        choices=("show", "sync"),
                        help="'show' prints the table (default); 'sync' runs "
                             "the Codex rollout reader into openai.json and "
                             "the opencode Go reader into opencode.json.")
    parser.add_argument("--json", action="store_true",
                        help="emit the same rows as JSON.")
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


def run_usage(argv) -> int:
    """Entry point registered as the ``usage`` verb in main._VERB_HANDLERS."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.action == "sync":
        return _run_sync(args)
    return _run_show(args)
