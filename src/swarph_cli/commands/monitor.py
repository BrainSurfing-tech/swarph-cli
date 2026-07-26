"""``swarph monitor`` — observe mesh DMs, deliver them to named, pluggable sinks.

Card #122. The engine (state model, sinks, ledgers, pidfile) lives in
``commands/mesh.py`` next to the gateway primitives it drives; this module is the
CLI surface only, so there is exactly one implementation under both this verb and
its deprecated alias ``swarph mesh sidecar``.

    swarph monitor start  [--deliver SINK]... [--poll-s N] [--wake-min-interval-s N]
    swarph monitor status [--json] [--brief]
    swarph monitor stop

WHY THIS VERB EXISTS. ``mesh sidecar`` had exactly one delivery mechanism: poke a
tmux pane. With only one sink, nothing forced anyone to separate "I read this
message" from "somebody was told about it", so ``last_msg_id`` came to mean both
and a dead pane froze the cursor forever (PR #138 fixed the symptom; this fixes
the cause).

WHY ``pull`` IS THE DEFAULT. Every PUSH sink's liveness is a precondition for
hearing anything, and the mesh has gone silently deaf twice that way: a tmux crash
or resume kills the wake Monitor, SessionStart drains but does not re-arm, and the
cell reaches a state indistinguishable from "no mail has arrived". A pull check
run BY the cell (``swarph monitor status``) lives one layer above tmux and
therefore cannot die with it. The honest limit: pull only fires when the cell is
awake enough to run it, which is exactly what push is for — so they compose.
``tmux:`` is the optimization; status-pull is the guarantee.

Designed for a SessionStart hook:

    swarph monitor start && swarph monitor status --brief

``start`` is idempotent, fast and silent when already running; ``status --brief``
prints nothing at all when there is nothing to say.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import mesh


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph monitor",
        description="Observe mesh DMs and deliver them to pluggable sinks.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="start the monitor (idempotent)")
    start.add_argument(
        "--deliver",
        action="append",
        default=[],
        metavar="SINK",
        help="repeatable; pull (default) | tmux:<target> | stdout | none. "
        "Each sink gets its OWN delivery ledger.",
    )
    start.add_argument("--poll-s", type=int, default=mesh._DEFAULT_POLL_S)
    start.add_argument(
        "--wake-min-interval-s",
        type=int,
        default=mesh._DEFAULT_WAKE_MIN_INTERVAL_S,
        help="minimum seconds between pushes to any one sink",
    )
    start.add_argument(
        "--replay-limit",
        type=int,
        default=mesh._MONITOR_REPLAY_LIMIT,
        help="max DMs a lagging/late-attached sink replays from inbox.log "
        f"(default {mesh._MONITOR_REPLAY_LIMIT}); what is skipped is REPORTED",
    )
    start.add_argument(
        "--foreground",
        action="store_true",
        help="run the poll loop inline instead of detaching (for supervisors)",
    )
    start.add_argument("--once", action="store_true", help="poll once and exit")
    _add_common(start)

    status = sub.add_parser("status", help="what has been observed vs delivered")
    status.add_argument("--json", action="store_true", help="print raw JSON")
    status.add_argument(
        "--brief",
        action="store_true",
        help="one line when there is something; NOTHING when there is not",
    )
    _add_common(status)

    stop = sub.add_parser("stop", help="stop this peer's monitor")
    _add_common(stop)

    return p


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as", dest="self_name", default=None, help="self peer name")
    p.add_argument("--state-dir", default=None, help="state directory")
    p.add_argument(
        "--gateway",
        default=os.environ.get("MESH_GATEWAY_URL", mesh._DEFAULT_GATEWAY),
        help="mesh-gateway base URL",
    )
    p.add_argument("--token-file", default=None, help="explicit bearer token file")


def _resolve(args: argparse.Namespace) -> tuple[str, Path]:
    state_dir_arg = Path(args.state_dir).expanduser() if args.state_dir else None
    self_name = mesh._resolve_self_name(args.self_name, state_dir=state_dir_arg)
    return self_name, state_dir_arg or mesh._default_sidecar_state_dir(self_name)


def _self_name_was_derived(args: argparse.Namespace) -> bool:
    """True when the identity came from the STATE DIR BASENAME, not the operator.

    That is the dangerous path: `--as` and $SWARPH_SELF are deliberate, a
    directory name is incidental.
    """
    return not args.self_name and not os.environ.get("SWARPH_SELF")


def _verify_self_is_registered(self_name, gateway, token):
    """Confirm the resolved identity is a real peer. Returns (ok, detail).

    THE FOOTGUN (droplet, deploying 0.39.0 on his box): self_name falls back to
    the state dir's BASENAME. He ran

        swarph monitor start --state-dir /var/lib/swarph/droplet-monitor

    and got a monitor for the peer `droplet-monitor`, WHICH DOES NOT EXIST. The
    gateway had 0 DMs addressed to it, because nobody can address a peer that is
    not registered. So it polled a nonexistent inbox, saw zero DMs forever, and
    reported itself RUNNING AND HEALTHY.

    That is THE EXACT FAILURE THIS CARD EXISTS TO REMOVE -- silent deafness --
    reintroduced through CONFIGURATION rather than through code. A dead sink and
    a nonexistent inbox emit the same signal as a quiet mesh: nothing.

    NETWORK FAILURE MUST NOT BLOCK: `start` is contractually safe to call
    unconditionally from a SessionStart hook, so an unreachable gateway warns
    and proceeds. We refuse only on a POSITIVE answer that the peer is absent.
    """
    url = f"{gateway.rstrip('/')}/peers"
    status, body = mesh._http_get_json(url, token)
    if status != 200 or not isinstance(body, dict):
        return (True, f"UNVERIFIED (gateway {status or 'unreachable'})")
    peers = body.get("peers")
    if not isinstance(peers, list):
        return (True, "UNVERIFIED (unexpected /peers shape)")
    names = {p.get("name") for p in peers if isinstance(p, dict)}
    if not names:
        return (True, "UNVERIFIED (empty peer list)")
    if self_name in names:
        return (True, "registered")
    return (False, f"NOT a registered peer (gateway knows {len(names)} peers)")


# ── start ────────────────────────────────────────────────────────────────────

_DAEMON_PIDFILE = "daemon.pid"


def _daemon_owns_state_dir(state_dir):
    """Detect a `swarph daemon` already writing this state dir.

    Returns (reason, detail) or (None, None).

    WHY THIS EXISTS (droplet's PR #139 review, live on his box): `swarph daemon`
    uses the IDENTICAL layout -- state_dir/cursor.json + state_dir/inbox.log.
    `_MONITOR_PIDFILE` guards monitor-against-monitor; NOTHING guarded
    monitor-against-daemon. So `swarph monitor start --state-dir
    /var/lib/swarph/<peer>` -- the obvious thing to type, because that is where
    the state already is -- puts two processes on one cursor, each advancing it
    on its own poll. Interleaved writes give lost DMs or repeats, and BOTH
    failure modes are silent.

    Two detectors, because one is not enough:
      1. `daemon.pid` -- reliable, but only for daemons started AFTER this
         landed. Every box already running one has no pidfile.
      2. `tasks_snapshot` in cursor.json -- the daemon seeds it and monitor
         NEVER writes it, so its presence is positive evidence a daemon owns
         this directory. This is what catches the already-running case.
    """
    pidfile = state_dir / _DAEMON_PIDFILE
    if pidfile.exists():
        try:
            rec = json.loads(pidfile.read_text(encoding="utf-8"))
            pid = int(rec.get("pid", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            pid = 0
        if pid and pid != os.getpid() and mesh._process_alive(pid):
            return ("live daemon pidfile", f"{pidfile} (pid {pid})")

    cursor = state_dir / "cursor.json"
    if cursor.exists():
        try:
            data = json.loads(cursor.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and "tasks_snapshot" in data:
            return ("daemon-owned cursor", f"{cursor} carries `tasks_snapshot`")
    return (None, None)


def _cmd_start(args: argparse.Namespace) -> int:
    # Parse sinks FIRST: a held or unknown sink must fail before anything is
    # created on disk, so a bad --deliver never leaves a half-started monitor.
    sinks = [mesh.parse_sink(spec) for spec in (args.deliver or ["pull"])]
    self_name, state_dir = _resolve(args)
    pidfile = state_dir / mesh._MONITOR_PIDFILE

    # REFUSE rather than race. droplet's call over silently defaulting to a
    # private state dir: sharing the directory is legitimate once one of the two
    # is stopped, so a hard error TEACHES the constraint instead of hiding it.
    # Same shape as the webhook hold -- loud refusal beats a silent race.
    reason, detail = _daemon_owns_state_dir(state_dir)
    if reason is not None:
        print(
            f"swarph monitor: REFUSING to start -- `swarph daemon` appears to own "
            f"{state_dir}\n"
            f"  evidence: {reason} -- {detail}\n"
            f"  Both write cursor.json and inbox.log. Two writers on one cursor "
            f"means lost DMs or repeats, silently.\n"
            f"  Either stop the daemon, or give the monitor its own "
            f"--state-dir.",
            file=sys.stderr,
        )
        return 2

    status, rec = mesh.pidfile_status(pidfile)
    if status == "live_ours":
        # THE HOOK PATH. This runs on every SessionStart, so it must cost
        # nothing and say nothing: no banner, no re-poll, exit 0.
        return 0

    # IDENTITY CHECK GOES HERE, NOT EARLIER. It costs a gateway round-trip, and
    # the `live_ours` path above is the SessionStart hook path -- contractually
    # free and silent. Putting this before that return made every session pay
    # for a network call and print on it; the pre-existing
    # test_start_is_quiet_and_zero_when_already_running caught that immediately.
    # An already-running monitor was verified when it started.
    # A monitor polling an inbox nobody can address is deaf while reporting
    # healthy -- see _verify_self_is_registered. Only the DERIVED path is
    # refused: an operator who typed `--as X` meant X, and may legitimately be
    # pre-staging a peer that is not registered yet.
    ok, detail = _verify_self_is_registered(self_name, args.gateway, mesh._resolve_token(self_name, args.token_file))
    if not ok:
        derived = _self_name_was_derived(args)
        print(
            f"swarph monitor: identity {self_name!r} is {detail}.\n"
            f"  Nothing can be addressed to it, so this monitor would see zero "
            f"DMs FOREVER and report itself healthy -- silent deafness.",
            file=sys.stderr,
        )
        if derived:
            print(
                f"  It was DERIVED from the state dir basename "
                f"({state_dir.name!r}), not chosen. Pass --as <peer> "
                f"(or set $SWARPH_SELF).",
                file=sys.stderr,
            )
            return 2
        print("  Continuing because you named it explicitly with --as.",
              file=sys.stderr)
    elif detail != "registered":
        print(f"swarph monitor: identity {self_name!r} {detail} -- "
              f"continuing (a hook must not fail on a down gateway).",
              file=sys.stderr)

    if status == "stale":
        # Reclaiming silently is fine right up until the day it was not stale.
        print(
            f"swarph monitor: reclaiming STALE pidfile {pidfile} "
            f"(pid {rec['pid']} is gone)",
            file=sys.stderr,
        )
    elif status == "foreign":
        # Adopting a live PID we cannot prove is ours is how `stop` ends up
        # killing something unrelated. Take the FILE, never the PROCESS.
        print(
            f"swarph monitor: pidfile {pidfile} names pid {rec['pid']}, which is "
            "ALIVE but is NOT this monitor (cmdline mismatch). NOT adopting it — "
            "reclaiming the pidfile only; that process is left alone.",
            file=sys.stderr,
        )

    token = mesh._resolve_token(self_name, args.token_file)
    state = mesh.MonitorState(
        self_name=self_name,
        state_dir=state_dir,
        gateway=args.gateway,
        token=token,
        sinks=sinks,
        poll_s=args.poll_s,
        min_interval_s=args.wake_min_interval_s,
        replay_limit=args.replay_limit,
    )
    # Only PUSH sinks replay, and only a pre-existing archive can surprise you.
    # Warning on a first-ever `--deliver pull` run (the default!) would be a
    # warning that fires when nothing is wrong — which is how warnings stop
    # being read at all.
    if state.inbox_log_path.exists():
        for sink in state.sinks:
            if sink.is_push and sink.name in state.new_ledgers:
                print(
                    f"swarph monitor: sink {sink.name} has no ledger yet — it "
                    f"starts empty and will replay up to {args.replay_limit} "
                    f"DM(s) from {state.inbox_log_path}",
                    file=sys.stderr,
                )

    if args.once:
        mesh.write_pidfile(
            pidfile, self_name=self_name, sinks=sinks, poll_s=args.poll_s
        )
        mesh._monitor_iteration(state)
        return 0

    if args.foreground or not hasattr(os, "fork"):
        if not hasattr(os, "fork"):
            print("swarph monitor: no fork() on this platform — running in the "
                  "foreground", file=sys.stderr)
        return _run_pinned(state, pidfile, self_name, sinks, args.poll_s)

    return _start_detached(state, pidfile, self_name, sinks, args.poll_s)


def _run_pinned(state, pidfile: Path, self_name: str, sinks: list, poll_s: int) -> int:
    mesh.write_pidfile(pidfile, self_name=self_name, sinks=sinks, poll_s=poll_s)
    try:
        return mesh._monitor_loop(state)
    finally:
        # Only clear the pidfile if it is still OURS — a racing `start` may have
        # legitimately reclaimed it, and deleting its record would strand it.
        if mesh.pidfile_status(pidfile)[0] == "live_ours":
            pidfile.unlink(missing_ok=True)


def _start_detached(state, pidfile: Path, self_name: str, sinks: list, poll_s: int) -> int:
    log_path = state.state_dir / "monitor.log"
    state.state_dir.mkdir(parents=True, exist_ok=True)
    pid = os.fork()
    if pid > 0:
        print(
            f"swarph monitor: started pid={pid} "
            f"sinks={','.join(s.name for s in sinks)} log={log_path}",
            file=sys.stderr,
        )
        return 0
    # ── child ──
    try:
        os.setsid()
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        mesh.write_pidfile(pidfile, self_name=self_name, sinks=sinks, poll_s=poll_s)
        mesh._monitor_loop(state)
    finally:
        # os._exit, never sys.exit: the child must not run the parent's atexit
        # handlers or unwind pytest/atexit state it never owned.
        os._exit(0)


# ── status ───────────────────────────────────────────────────────────────────

def _collect(args: argparse.Namespace) -> dict:
    """Everything `status` reports, read from DISK — no gateway, no token.

    Deliberate: the whole point of pull is that it still answers when the push
    path (and the network) is dead. `status` is at most one poll stale.
    """
    self_name, state_dir = _resolve(args)
    pidfile = state_dir / mesh._MONITOR_PIDFILE
    pstatus, rec = mesh.pidfile_status(pidfile)
    cursor = mesh._read_cursor(state_dir / "cursor.json")
    observed = int(cursor.get("last_msg_id", 0))
    ledgers = mesh._read_ledgers(state_dir / "ledgers.json")
    inbox_log = state_dir / "inbox.log"

    sinks = []
    for spec in (rec or {}).get("sinks") or []:
        try:
            sinks.append(mesh.parse_sink(str(spec)))
        except mesh.MonitorSinkError:
            continue

    rows = []
    for sink in sinks:
        if not sink.keeps_ledger:
            continue
        led = ledgers.get(sink.name)
        delivered = int(led["last_delivered_id"]) if led else 0
        dms, skipped = mesh._replay_from_inbox_log(
            inbox_log, delivered, mesh._MONITOR_REPLAY_LIMIT
        )
        rows.append({
            "name": sink.name,
            "keeps_ledger": True,
            "is_push": sink.is_push,
            "last_delivered_id": delivered,
            "last_delivery_at": float(led["last_delivery_at"]) if led else 0.0,
            "consecutive_failures": int(led["consecutive_failures"]) if led else 0,
            "ledger_missing": led is None,
            "pending": len(dms) + skipped,
            "pending_from": sorted({str(d.get("from_node")) for d in dms}),
            "label": sink.pending_label(len(dms) + skipped),
        })

    return {
        "self": self_name,
        "state_dir": str(state_dir),
        "running": pstatus == "live_ours",
        "pidfile_status": pstatus,
        "pid": (rec or {}).get("pid"),
        "configured_sinks": [s.name for s in sinks],
        "observation_cursor": observed,
        # `none` keeps no ledger, so there is nothing to subtract. Saying "0
        # unread" here would recreate the exact defect this card removes: an
        # absence that reads as evidence.
        "unread_reportable": bool(rows),
        "sinks": rows,
    }


def _cmd_status(args: argparse.Namespace) -> int:
    info = _collect(args)
    pending = sum(r["pending"] for r in info["sinks"])

    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
    elif args.brief:
        _print_brief(info, pending)
    else:
        _print_status(info, pending)

    if not info["running"]:
        return 2
    if not info["unread_reportable"]:
        return 0
    return 1 if pending else 0


def _print_brief(info: dict, pending: int) -> None:
    if not info["running"]:
        print("swarph monitor: not running — run: swarph monitor start")
        return
    if not info["unread_reportable"]:
        # NOT silence: silence in a hook reads as "0 unread", which is the
        # defect. Say that we cannot tell.
        print("swarph monitor: --deliver none keeps no ledger — unread CANNOT be "
              "reported (this is not zero)")
        return
    if not pending:
        return          # nothing to say; a hook must not spam every session
    parts = [f"{r['label']} ({', '.join(r['pending_from'])})" if r["pending_from"]
             else r["label"]
             for r in info["sinks"] if r["pending"]]
    print("; ".join(parts) + " — swarph mesh inbox")


def _print_status(info: dict, pending: int) -> None:
    if not info["running"]:
        extra = {
            "stale": " (stale pidfile: the recorded pid is gone)",
            "foreign": " (pidfile names a live pid that is NOT this monitor)",
        }.get(info["pidfile_status"], "")
        print(f"monitor {info['self']}: not running{extra}")
        print(f"  state: {info['state_dir']}")
        print(f"  observation cursor: last_msg_id={info['observation_cursor']}")
        print("  start it with: swarph monitor start")
        return

    print(f"monitor {info['self']}: running pid={info['pid']} "
          f"sinks={','.join(info['configured_sinks']) or '(none configured)'}")
    print(f"  state: {info['state_dir']}")
    print(f"  observation cursor: last_msg_id={info['observation_cursor']}")

    if not info["unread_reportable"]:
        print("  unread: CANNOT REPORT — no configured sink keeps a delivery "
              "ledger (`--deliver none` keeps none by design), so there is "
              "nothing to subtract from the cursor.")
        print("  This is NOT zero unread; it is not tracked. Add "
              "`--deliver pull` to make it reportable.")
        return

    for row in info["sinks"]:
        who = f" ({', '.join(row['pending_from'])})" if row["pending_from"] else ""
        print(f"  {row['name']}: {row['label']}{who} "
              f"[last_delivered_id={row['last_delivered_id']} "
              f"failures={row['consecutive_failures']}]")
        if row["ledger_missing"] and row["is_push"]:
            print(f"    NOTE: no ledger on disk for {row['name']} yet — ledgers "
                  "are keyed by the sink STRING, so a renamed target starts "
                  "fresh and replays. This is why the count above may surprise "
                  "you.")
    if pending:
        print("  read them with: swarph mesh inbox")


# ── stop ─────────────────────────────────────────────────────────────────────

def _cmd_stop(args: argparse.Namespace) -> int:
    _self_name, state_dir = _resolve(args)
    pidfile = state_dir / mesh._MONITOR_PIDFILE
    status, rec = mesh.pidfile_status(pidfile)

    if status == "absent":
        print("swarph monitor: not running (no pidfile)")
        return 0
    if status == "stale":
        pidfile.unlink(missing_ok=True)
        print(f"swarph monitor: not running (stale pidfile for pid "
              f"{rec['pid']} removed)")
        return 0
    if status == "foreign":
        print(
            f"swarph monitor: REFUSING to stop pid {rec['pid']} — it is alive but "
            "is NOT this monitor (cmdline mismatch). Signalling it could kill "
            f"something unrelated. Remove {pidfile} by hand if you are sure.",
            file=sys.stderr,
        )
        return 2

    mesh._terminate(int(rec["pid"]))
    pidfile.unlink(missing_ok=True)
    print(f"swarph monitor: stopped pid {rec['pid']}. Owed deliveries are "
          "ABANDONED, not flushed — every ledger persists on disk, so the next "
          "`swarph monitor start` resumes exactly where this one stopped.")
    return 0


def run_monitor(argv: list[str]) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "start":
            return _cmd_start(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "stop":
            return _cmd_stop(args)
        parser.error(f"unknown command: {args.command}")
    except mesh.MonitorSinkError as exc:
        print(f"swarph monitor: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"swarph monitor: {exc}", file=sys.stderr)
        return 2
    return 2
