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
import re
import subprocess
import sys
import time
from pathlib import Path

from swarph_cli.gateway_default import env_gateway

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
        help="repeatable; pull (default) | tmux:<target> | tmux-notify:<target> "
        "| stdout | none. "
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
    start.add_argument(
        "--supervisor",
        default=None,
        metavar="SPEC",
        help="who supervises this process (e.g. task:Swarph cursor-win "
        "Monitor). Recorded in the pidfile so `status` can answer "
        "\"what supervises this pid\" — Windows has no pid→task reverse "
        "map, so ownership is this convention or nothing (#644). Default: "
        "$SWARPH_SUPERVISOR, read at RUN time (a parser-built default would "
        "freeze whatever the builder's environment held).",
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

    hcheck = sub.add_parser(
        "heartbeat-check",
        help="#544 Proposal A/B: independently verify the drain heartbeat and "
             "escalate DEGRADED, with cause named, to the gateway",
    )
    hcheck.add_argument(
        "--stale-after-s",
        type=int,
        default=None,
        help="heartbeat age past which the monitor is considered not draining. "
             "DEFAULT IS DERIVED from the running monitor's own poll interval "
             "(6 intervals, floor 60s), NOT a constant -- a threshold shorter "
             "than the poll interval reports DEGRADED forever. An explicit "
             "value below 2 intervals is REFUSED for that reason.",
    )
    _add_common(hcheck)

    install = sub.add_parser(
        "install-task",
        help="#644: register the Windows Task Scheduler runner+watchdog pair "
             "that supervises this peer's monitor (Windows only)",
    )
    install.add_argument(
        "--deliver",
        action="append",
        default=[],
        metavar="SINK",
        help="sink(s) the supervised monitor delivers to — same syntax as "
             "`start`. Recorded in the runner task's action.",
    )
    install.add_argument(
        "--watchdog-min",
        type=int,
        default=5,
        help="watchdog interval in minutes (default 5). The watchdog is "
             "LOAD-BEARING: restart-on-failure is exit-code keyed, so an "
             "exit-0 crash loop is invisible to the runner task — exactly "
             "#636's shape. The watchdog is what catches it.",
    )
    install.add_argument(
        "--swarph-bin",
        default=None,
        help="explicit swarph executable the tasks invoke (default: whatever "
             "swarph.exe resolves on PATH). Needed to supervise a NON-default "
             "build — e.g. demonstrating a branch on metal before release.",
    )
    install.add_argument(
        "--start",
        action="store_true",
        help="start the runner task immediately after registering",
    )
    install.add_argument(
        "--print-path",
        action="store_true",
        help="print the packaged installer script path and exit",
    )
    _add_common(install)

    return p


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as", dest="self_name", default=None, help="self peer name")
    p.add_argument("--state-dir", default=None, help="state directory")
    p.add_argument(
        "--gateway",
        default=env_gateway(),
        help="mesh-gateway base URL",
    )
    p.add_argument("--token-file", default=None, help="explicit bearer token file")


def _resolve(args: argparse.Namespace) -> tuple[str, Path]:
    state_dir_arg = Path(args.state_dir).expanduser() if args.state_dir else None
    self_name = mesh._resolve_self_name(args.self_name, state_dir=state_dir_arg)
    return self_name, state_dir_arg or mesh._default_sidecar_state_dir(self_name)


def _resolve_supervisor(args: argparse.Namespace) -> "str | None":
    """The flag wins; $SWARPH_SUPERVISOR is the fallback. Read at RUN time —
    a scheduled task's launcher sets the env, an operator types the flag."""
    return args.supervisor or os.environ.get("SWARPH_SUPERVISOR")


def _read_cgroup(pid: int) -> "str | None":
    """/proc/<pid>/cgroup, or None anywhere it cannot be read (Windows,
    restricted procfs, dead pid). Module-level so tests can stub the seam."""
    try:
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None


def _derive_systemd_unit(pid: "int | None") -> "str | None":
    """The supervisor Linux already knows (#344 review): Windows has no
    pid→task map so the pidfile CLAIM is the whole answer there, but on
    Linux /proc/<pid>/cgroup names the owning unit — and no fleet unit sets
    SWARPH_SUPERVISOR, so without this every systemd-owned monitor prints
    ORPHAN while its cgroup says otherwise. Derive where you can; assert
    only where you must."""
    if pid is None:
        return None
    text = _read_cgroup(pid)
    if not text:
        return None
    for line in text.splitlines():
        # v2: "0::/system.slice/swarph-monitor@cell.service"; v1 carries the
        # same unit path per subsystem. The leaf is the unit name.
        leaf = line.rsplit("/", 1)[-1].strip()
        if leaf.endswith(".service"):
            return leaf[: -len(".service")]
    return None


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

# A deliberate `monitor stop` writes this marker; the #644 watchdog refuses to
# revive a held monitor and `monitor start` (any path) clears it. systemd
# semantics: `systemctl stop` is not a failure, so restart policy must not
# treat it as one — a watchdog that revives a deliberately-stopped monitor is
# the same class of surprise as a second instance beside a hand-started one.
_SUPERVISION_HOLD = "supervision_hold.json"


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

    supervisor = _resolve_supervisor(args)
    hold_path = state_dir / _SUPERVISION_HOLD
    if supervisor is None:
        # An OPERATOR's start is an un-hold: a human asked for running, so
        # the deliberate-stop marker must not outlive the request.
        hold_path.unlink(missing_ok=True)
    elif hold_path.exists():
        # A supervisor's retry is NOT an operator's request (#344 review):
        # `monitor stop` under the runner exits 15, restart-on-failure fires
        # within a minute, and if this start cleared the hold the deliberate
        # stop would be revived and the evidence erased with it. The runner
        # launcher gates on the hold before ever calling this; this is the
        # backstop. Exit 0 — a non-zero here would restart-loop the runner.
        print(
            "swarph monitor: supervision HOLD present and this start is "
            f"supervised ({supervisor}) — NOT starting; the hold stands. "
            "An operator clears it with `swarph monitor start` (no "
            "--supervisor, no $SWARPH_SUPERVISOR).",
            file=sys.stderr,
        )
        return 0

    if args.once:
        mesh.write_pidfile(
            pidfile, self_name=self_name, sinks=sinks, poll_s=args.poll_s,
            supervisor=supervisor,
        )
        mesh._monitor_iteration(state)
        return 0

    if args.foreground or not hasattr(os, "fork"):
        if not hasattr(os, "fork"):
            print("swarph monitor: no fork() on this platform — running in the "
                  "foreground", file=sys.stderr)
        return _run_pinned(state, pidfile, self_name, sinks, args.poll_s,
                           supervisor=supervisor)

    return _start_detached(state, pidfile, self_name, sinks, args.poll_s,
                           supervisor=supervisor)


def _run_pinned(state, pidfile: Path, self_name: str, sinks: list, poll_s: int,
                supervisor: "str | None" = None) -> int:
    mesh.write_pidfile(pidfile, self_name=self_name, sinks=sinks, poll_s=poll_s,
                       supervisor=supervisor)
    try:
        return mesh._monitor_loop(state)
    finally:
        # Only clear the pidfile if it is still OURS — a racing `start` may have
        # legitimately reclaimed it, and deleting its record would strand it.
        if mesh.pidfile_status(pidfile)[0] == "live_ours":
            pidfile.unlink(missing_ok=True)


def _start_detached(state, pidfile: Path, self_name: str, sinks: list, poll_s: int,
                    supervisor: "str | None" = None) -> int:
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
        mesh.write_pidfile(pidfile, self_name=self_name, sinks=sinks, poll_s=poll_s,
                           supervisor=supervisor)
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
        # The RUNNING monitor's own poll interval, recorded by write_pidfile.
        # heartbeat-check derives its staleness threshold from this instead of
        # inventing a constant -- see _resolve_stale_after.
        "poll_s": (rec or {}).get("poll_s"),
        # When the LIVE writer started -- lets heartbeat-check tell a writer
        # that has had time to emit and did not (feature absent) from one that
        # has only just come up.
        "started_at": (rec or {}).get("started_at"),
        # The writer's OWN declaration that it can emit a heartbeat. Absent =>
        # a build predating the feature. Established independently of the
        # heartbeat file, because inferring it from that file is circular.
        "emits_heartbeat": (rec or {}).get("emits_heartbeat"),
        # #644 ownership: who CLAIMS this process. None means hand-started or
        # predates the feature — an ORPHAN, which status must SAY, not omit.
        "supervisor": (rec or {}).get("supervisor"),
        # ...and who the OS says owns it, where the OS can answer (#344
        # review): on Linux the cgroup names the systemd unit even when no
        # claim was recorded. None on Windows or unreadable procfs.
        "supervisor_derived": (
            _derive_systemd_unit((rec or {}).get("pid"))
            if pstatus == "live_ours" else None
        ),
        "supervision_hold": (state_dir / _SUPERVISION_HOLD).exists(),
        "configured_sinks": [s.name for s in sinks],
        "observation_cursor": observed,
        # `none` keeps no ledger, so there is nothing to subtract. Saying "0
        # unread" here would recreate the exact defect this card removes: an
        # absence that reads as evidence.
        "unread_reportable": bool(rows),
        "sinks": rows,
        "pending_channel_posts": cursor.get("pending_channel_posts", []),
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
    print("; ".join(parts) + f" — swarph mesh inbox --as {info['self']}")


def _print_status(info: dict, pending: int) -> None:
    if not info["running"]:
        extra = {
            "stale": " (stale pidfile: the recorded pid is gone)",
            "foreign": " (pidfile names a live pid that is NOT this monitor)",
        }.get(info["pidfile_status"], "")
        print(f"monitor {info['self']}: not running{extra}")
        print(f"  state: {info['state_dir']}")
        print(f"  observation cursor: last_msg_id={info['observation_cursor']}")
        if info.get("supervision_hold"):
            print("  supervision HOLD present (deliberate `monitor stop`) — the "
                  "watchdog will not revive it; `swarph monitor start` clears "
                  "the hold.")
        else:
            print("  start it with: swarph monitor start")
        return

    print(f"monitor {info['self']}: running pid={info['pid']} "
          f"sinks={','.join(info['configured_sinks']) or '(none configured)'}")
    claim = info.get("supervisor")
    derived = info.get("supervisor_derived")
    if claim:
        print(f"  supervised by: {claim}")
        if derived and derived not in claim:
            # A claim that contradicts the cgroup is the divergence the
            # ownership census (#517) exists to NAME — print both, never
            # pick one silently.
            print(f"  cgroup reports: systemd:{derived} — claim and cgroup "
                  "DIVERGE; the pidfile claim is stale or the unit was "
                  "restarted under a different name")
    elif derived:
        print(f"  supervised by: systemd:{derived} (derived from cgroup)")
    else:
        print("  supervised by: NOTHING ON RECORD — hand-started or predates "
              "#644 (ORPHAN)")
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

    if info.get("pending_channel_posts"):
        n = len(info["pending_channel_posts"])
        channels = sorted({p["channel"] for p in info["pending_channel_posts"]})
        plural = "s" if n != 1 else ""
        print(f"  {n} unread channel post{plural} in: {', '.join(channels)}")

    if pending:
        print(f"  read them with: swarph mesh inbox --as {info['self']}")


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
    mesh._write_cursor_atomic(
        state_dir / _SUPERVISION_HOLD,
        {"since": time.time(), "by": "swarph monitor stop"},
    )
    print(f"swarph monitor: stopped pid {rec['pid']}. Owed deliveries are "
          "ABANDONED, not flushed — every ledger persists on disk, so the next "
          "`swarph monitor start` resumes exactly where this one stopped.")
    print("swarph monitor: supervision HOLD written — a #644 watchdog will NOT "
          "revive this monitor. `swarph monitor start` clears the hold.")
    return 0


# ── heartbeat-check (#544 Proposal A/B) ────────────────────────────────────

_CAUSE_PATTERNS = [
    (re.compile(r"UnicodeEncodeError|UnicodeDecodeError|charmap"), "encoding"),
    (re.compile(r"\b40[13]\b|Unauthorized|Forbidden"), "auth"),
    (re.compile(r"\b429\b|rate.?limit", re.IGNORECASE), "quota"),
    (re.compile(r"gateway 5\d\d"), "gateway"),
    (re.compile(r"MonitorSinkError|SinkError"), "sink"),
]


def _unit_identity(unit: str) -> "str | None":
    """The cell a unit runs as, or None when it names NOBODY.

    >>> `--as` IS NOT THE ONLY WAY A MONITOR GETS ITS IDENTITY. <<< (lab-ovh,
    measured, DM 25772.) `_self_name_was_derived` shows $SWARPH_SELF alone is
    sufficient, so a unit carrying `Environment=SWARPH_SELF=<PEER>` and NO
    `--as` runs perfectly -- and was invisible to the ExecStart-only probe this
    replaces, silently dropping out of its own check.

    The hole is REACHABLE, not theoretical: the shipped unit sets BOTH, so the
    `--as` flag reads as redundant to anyone tidying that file, and deleting it
    leaves a working monitor nothing can attribute. The redundancy inviting the
    edit lives in the very file the check depends on.

    Returns None for a unit naming neither -- the caller must treat that as
    NOT ATTRIBUTABLE (a third state), never fold it into "not mine".
    """
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "ExecStart", "-p", "Environment"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        text = out.stdout or ""
    except OSError:
        text = ""
    m = re.search(r"--as[\s=]+([\w.-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"SWARPH_SELF=([\w.-]+)", text)
    return m.group(1) if m else None


def _unit_names_this_cell(unit: str, self_name: str) -> bool:
    """Does this unit provably run as THIS cell? THE UNIT NAME IS A PROXY.

    A unit NAME suggests ownership; the invocation (`--as`, or $SWARPH_SELF in
    its environment) PROVES it. Trusting the name is the same error class as
    trusting $SWARPH_SELF inherited from a multiplexer server (cursor-win's
    psmux identity leak).
    """
    return _unit_identity(unit) == self_name


def _unit_exists(unit: str) -> bool:
    """Whether systemd knows this unit at all — used by the guard suite to tell
    a real negative from a vacuous one (no unit to misattribute is not a pass)."""
    try:
        out = subprocess.run(
            ["systemctl", "list-unit-files", unit, "--no-legend", "--no-pager"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return False
    if unit in (out.stdout or ""):
        return True
    # A unit can be loaded without a unit FILE (runtime/transient), so ask the
    # other way too rather than reporting a false absence.
    probe = subprocess.run(
        ["systemctl", "show", unit, "-p", "LoadState", "--value"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (probe.stdout or "").strip() == "loaded"


def _candidate_units(self_name: str) -> list:
    """Units that may supervise THIS cell — ownership proved, never assumed.

    >>> FOUND BY INDUCING A REAL OUTAGE (science-claude, 2026-08-21). <<< This
    used to fall back to the bare `swarph-monitor.service`. On lab-ovh that unit
    is LAB-OVH'S MONITOR (`--as lab-ovh`, measured). So stopping science-claude's
    own unit produced `cause=unrecognized`: the probe found lab's unit active,
    scanned LAB'S JOURNAL, matched nothing, and reported a plausible-looking
    wrong answer instead of the true `supervisor_absent`.

    That is this card's own defect committed by this card's own detector —
    reading ANOTHER CELL'S state and reporting it as this one's. The template's
    header already warns why: "A GENERIC SERVICE NAME ON A MULTI-CELL HOST IS A
    COLLISION WAITING FOR A RESTART." A generic name cannot be attributed to any
    particular cell, so it is never accepted on the strength of its name.

    The template INSTANCE is safe by construction (%i IS the cell name). Anything
    else must prove itself, which also covers legitimately non-templated units
    like `swarph-monitor-gridiron.service`.
    """
    units, _unattributable = _partition_units(self_name)
    return units


def _partition_units(self_name: str) -> tuple:
    """(mine, unattributable) — THREE states, never two.

    attributable-to-me · attributable-to-another · NOT ATTRIBUTABLE. lab-ovh's
    correction (DM 25772): silently dropping the third folds it into the second,
    which is this card's own Family B-DUAL defect. An ACTIVE swarph-monitor unit
    naming nobody might be this cell's supervisor, so concluding
    `supervisor_absent` while one exists is a determinate negative the evidence
    does not support.
    """
    mine = [f"swarph-monitor@{self_name}.service"]
    unattributable = []
    try:
        listing = subprocess.run(
            ["systemctl", "list-units", "swarph-monitor*", "--all",
             "--no-legend", "--no-pager", "--plain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return mine, unattributable
    for line in (listing.stdout or "").splitlines():
        name = line.split()[0] if line.split() else ""
        if not name.endswith(".service") or name in mine:
            continue
        who = _unit_identity(name)
        if who == self_name:
            mine.append(name)
        elif who is None:
            unattributable.append(name)
    return mine, unattributable


def _classify_drain_failure(self_name: str, pidfile_status: str,
                            hb: dict = None, live_pid=None,
                            writer_uptime_s=None, interval_s: int = 30,
                            emits_heartbeat=None) -> str:
    """Name the cause from OUTSIDE the dead process.

    Never guesses among the journal-derived causes without a matching pattern in
    the process's own error text -- an unmatched stale-with-live-supervisor case
    is reported `unrecognized` rather than forced into the nearest label, per the
    design's own non-vacuity requirement.

    >>> SEVENTH CAUSE, FOUND BY INDUCING A REAL OUTAGE (2026-08-21): THE WRITER
    MAY SIMPLY NOT HAVE THE FEATURE. <<< The design named six causes and folded
    this one into `silent_hang`, because a hung writer and a writer that never
    implements the heartbeat produce THE IDENTICAL ARTEFACT: a file that does not
    advance. Measured here: lab-ovh's shared editable clone sits on `main`, which
    carries ZERO occurrences of `drain_heartbeat`, so science-claude's supervised
    monitor CANNOT write one -- and heartbeat-check called that `silent_hang`, a
    confident wrong answer on a perfectly healthy cell.

    That is not an edge case; DURING ANY ROLLOUT IT IS THE MAJORITY CASE. Every
    cell not yet upgraded reports DEGRADED, the fleet goes red at once, and a
    permanent red trains readers to skip the row -- obligation_sweep.py's own
    recorded lesson, reproduced by the detector meant to prevent it.

    `pid` in the heartbeat is what separates them: if the LIVE writer has been up
    longer than a couple of intervals and the newest heartbeat still carries a
    DIFFERENT pid, that writer has had time to write one and has not -- it does
    not have the feature. Same shape as Family B-DUAL's law: one message for two
    causes hides which one you have, so the state gets SPLIT rather than
    special-cased.
    """
    # CAPABILITY FIRST, AND FROM THE WRITER'S OWN DECLARATION -- never inferred
    # from the heartbeat file (lab-ovh, DM 25744: asking the artifact whether the
    # artifact is supported is circular). A live writer whose pidfile carries no
    # `emits_heartbeat` key predates the feature and CANNOT emit one; that is a
    # different fact from a writer that can and stopped, and it resolves on the
    # FIRST check rather than after two intervals of waiting.
    if pidfile_status == "live_ours" and not emits_heartbeat:
        return "writer_lacks_heartbeat"

    if hb is None and pidfile_status == "live_ours":
        # A live capable writer has not completed an iteration yet.
        return "heartbeat_absent"

    # Secondary discriminator, kept for the case where the pidfile is stale or
    # foreign so no declaration is readable: a heartbeat carrying a DIFFERENT
    # pid than the live writer, which has had time to write one and has not.
    hb_pid = hb.get("pid")
    if (pidfile_status == "live_ours" and live_pid and hb_pid != live_pid
            and (writer_uptime_s or 0) > 2 * interval_s):
        return "writer_lacks_heartbeat"

    if pidfile_status == "live_ours":
        # pidfile says the process IS running, but its heartbeat has not
        # advanced -- the process exists but is not doing the work. No OS
        # supervisor catches this; it is exactly what A/B exist to catch.
        return "silent_hang"

    units, unattributable = _partition_units(self_name)
    for unit in units:
        try:
            probe = subprocess.run(
                ["systemctl", "is-active", unit], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except OSError:
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "active":
            try:
                journal = subprocess.run(
                    ["journalctl", "-u", unit, "-n", "80", "--no-pager"],
                    capture_output=True, text=True, timeout=5,
                    encoding="utf-8", errors="replace",
                )
                text = journal.stdout or ""
            except (subprocess.SubprocessError, OSError):
                text = ""
            for pattern, cause in _CAUSE_PATTERNS:
                if pattern.search(text):
                    return cause
            # A supervisor claims this cell is running and nothing in its
            # recent journal matches a known cause -- name that plainly
            # rather than assert a cause with no evidence behind it.
            return "unrecognized"

    # THE THIRD ATTRIBUTION STATE, before any determinate negative. An ACTIVE
    # swarph-monitor unit that names NOBODY may well be this cell's supervisor;
    # calling it absent asserts more than the evidence carries. CANNOT_EVALUATE
    # in the shape this verb can express (lab-ovh, DM 25772).
    for unit in unattributable or []:
        try:
            probe = subprocess.run(
                ["systemctl", "is-active", unit], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except OSError:
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "active":
            return "supervisor_unattributable"

    # No pidfile match, no active unit for this cell, and nothing ambiguous --
    # exactly workstation-lc's own death: a clean exit with nothing configured
    # to restart it. NOTE this is the NORMAL case on a box where most monitors
    # are started by hand (ensure_monitor.sh), not an anomaly: 7 of 10 on
    # lab-ovh have no unit at all.
    return "supervisor_absent"


def _register_capabilities(self_name: str, gateway: str, token_file, caps: dict) -> int:
    """Push capabilities via the SAME upsert path `swarph mesh register` uses.

    Not a new gateway surface -- `#544`'s Q1 resolution (droplet, AI² review,
    msg 25394) is that wake/drain state belongs on the existing peer
    capabilities dict, and this reuses it verbatim rather than adding a
    second write path for the same fact.
    """
    ns = argparse.Namespace(
        self_name=self_name,
        url=None,
        capability=[f"{k}={json.dumps(v)}" for k, v in caps.items()],
        force=True,
        replace=False,
        gateway=gateway,
        token_file=token_file,
    )
    return mesh._run_register(ns)


_DEFAULT_POLL_S_FALLBACK = 30      # only when no pidfile records the real one
_STALE_INTERVALS = 6               # how many missed polls before DEGRADED
_MIN_INTERVALS = 2                 # below this, DEGRADED is guaranteed, not measured


def _resolve_stale_after(explicit, poll_s) -> tuple[int, str]:
    """(threshold_seconds, how_it_was_decided). Raises on a guaranteed-red value.

    >>> A STALENESS THRESHOLD IS NOT AN INDEPENDENT CONSTANT. IT IS A FUNCTION
    OF THE POLL INTERVAL. <<< The heartbeat can only advance once per poll, so a
    threshold shorter than the interval reports DEGRADED on a perfectly healthy
    cell, every time, forever.

    FOUND BY RUNNING IT (2026-08-21): the first arm of this verb's own induced
    test used --stale-after-s 5 against a 30s poll and reported
    `DEGRADED cause=silent_hang` on a cell that was draining normally. The logic
    was right; the number was invented. That is the same defect lab-ovh made an
    hour earlier setting obligation due dates to 168h/120h/144h by feel, and the
    same one science-claude made choosing 180 here -- a number nobody derived.

    A permanent red is worse than no check: obligation_sweep.py's own docstring
    already records why ("a permanent red trains readers to skip the row, and
    then the real ones go unread too"). So an explicit sub-2-interval value is
    REFUSED rather than honoured -- the caller is asking for a detector that
    cannot come out negative.
    """
    interval = int(poll_s) if poll_s else _DEFAULT_POLL_S_FALLBACK
    if explicit is None:
        derived = max(_STALE_INTERVALS * interval, 60)
        src = (f"derived: {_STALE_INTERVALS} x {interval}s poll"
               + (" (poll interval UNKNOWN -- no pidfile, assumed "
                  f"{_DEFAULT_POLL_S_FALLBACK}s)" if not poll_s else ""))
        return derived, src
    if explicit < _MIN_INTERVALS * interval:
        raise RuntimeError(
            f"--stale-after-s {explicit} is below {_MIN_INTERVALS} poll "
            f"intervals ({_MIN_INTERVALS} x {interval}s = "
            f"{_MIN_INTERVALS * interval}s). The heartbeat advances at most "
            f"once per poll, so this threshold would report DEGRADED on a "
            f"HEALTHY cell every time -- a detector that cannot come out "
            f"negative. REFUSING rather than reporting a red it did not earn."
        )
    return explicit, "explicit"


def _cmd_heartbeat_check(args: argparse.Namespace) -> int:
    if not args.self_name:
        print(
            "swarph monitor heartbeat-check: refusing to run with an ambiently-"
            "resolved identity -- pass --as explicitly. Proposal A's own "
            "constraint: this must never trust inherited env for identity.",
            file=sys.stderr,
        )
        return 2

    info = _collect(args)
    self_name = info["self"]
    state_dir = Path(info["state_dir"])
    hb_path = state_dir / "drain_heartbeat.json"
    since_path = state_dir / "drain_degraded_since.json"

    now = time.time()
    last_ts = None
    hb = None
    try:
        hb = json.loads(hb_path.read_text(encoding="utf-8"))
        last_ts = float(hb["ts"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    age = (now - last_ts) if last_ts is not None else None
    stale_after_s, threshold_src = _resolve_stale_after(
        args.stale_after_s, info.get("poll_s"))
    stale = age is None or age > stale_after_s

    configured_sinks = info["configured_sinks"]
    caps = {
        "wake_sinks": ",".join(configured_sinks) or "(none)",
        "wake_route": (
            "push" if any(r["is_push"] for r in info["sinks"])
            else "pull" if any(s != "none" for s in configured_sinks)
            else "none"
        ),
    }

    if info.get("supervision_hold"):
        # A DELIBERATE stop is not an outage. Report HELD — a third state, not
        # folded into OK (the monitor is NOT draining) nor DEGRADED (nothing is
        # wrong; a red here trains readers to skip the row).
        since_path.unlink(missing_ok=True)
        caps["drain_status"] = "HELD"
        caps["degraded_cause"] = "deliberately_stopped"
        rc = _register_capabilities(self_name, args.gateway, args.token_file, caps)
        print(f"heartbeat-check {self_name}: HELD (deliberate stop — "
              f"{_SUPERVISION_HOLD} present; `monitor start` clears it)")
        return rc

    if not stale:
        since_path.unlink(missing_ok=True)
        caps["drain_status"] = "OK"
        caps["drain_heartbeat_age_s"] = round(age, 1)
        rc = _register_capabilities(self_name, args.gateway, args.token_file, caps)
        print(f"heartbeat-check {self_name}: OK age={age:.0f}s "
              f"(threshold {stale_after_s}s, {threshold_src})")
        return rc

    interval_s = int(info.get("poll_s") or _DEFAULT_POLL_S_FALLBACK)
    started_at = (info.get("started_at") or 0)
    cause = _classify_drain_failure(
        self_name, info["pidfile_status"], hb=hb, live_pid=info.get("pid"),
        writer_uptime_s=(now - started_at) if started_at else None,
        interval_s=interval_s, emits_heartbeat=info.get("emits_heartbeat"))
    try:
        since = float(json.loads(since_path.read_text(encoding="utf-8"))["since"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        since = now
        mesh._write_cursor_atomic(since_path, {"since": since})

    caps["drain_status"] = "DEGRADED"
    caps["degraded_cause"] = cause
    caps["degraded_since"] = since
    caps["drain_heartbeat_age_s"] = round(age, 1) if age is not None else None
    rc = _register_capabilities(self_name, args.gateway, args.token_file, caps)
    age_desc = f"{age:.0f}s" if age is not None else "never observed"
    print(f"heartbeat-check {self_name}: DEGRADED cause={cause} "
          f"age={age_desc} (threshold {stale_after_s}s, {threshold_src})",
          file=sys.stderr)
    return 1 if rc == 0 else rc


# ── install-task (#644 Windows supervision) ──────────────────────────────────

def _cmd_install_task(args: argparse.Namespace) -> int:
    """Register the runner+watchdog Task Scheduler pair for this peer.

    The mechanics live in a packaged .ps1 (ff53570's precedent: the waker
    installer) because ScheduledTasks cmdlets are the only sane registration
    surface; this verb is the discoverable, testable front door.
    """
    from importlib import resources

    script = resources.files("swarph_cli").joinpath(
        "scripts", "install_monitor_task_windows.ps1")
    if args.print_path:
        print(script)
        return 0
    if os.name != "nt":
        print(
            "swarph monitor install-task: Windows-only. On Linux the systemd "
            "units are the supervision layer (#517); this verb is the Windows "
            "equivalent (#644).",
            file=sys.stderr,
        )
        return 2

    self_name, state_dir = _resolve(args)
    # Parse sinks NOW so a bad --deliver fails before anything is registered.
    sinks = [mesh.parse_sink(spec) for spec in (args.deliver or ["pull"])]

    cmd = [
        "powershell.exe", "-NoLogo", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Peer", self_name,
        "-StateDir", str(state_dir),
        "-Gateway", args.gateway,
        "-WatchdogIntervalMinutes", str(args.watchdog_min),
    ]
    for sink in sinks:
        cmd += ["-Deliver", sink.name]
    if args.swarph_bin:
        cmd += ["-SwarphBin", args.swarph_bin]
    if args.start:
        cmd.append("-Start")
    return subprocess.run(cmd).returncode


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
        if args.command == "heartbeat-check":
            return _cmd_heartbeat_check(args)
        if args.command == "install-task":
            return _cmd_install_task(args)
        parser.error(f"unknown command: {args.command}")
    except mesh.MonitorSinkError as exc:
        print(f"swarph monitor: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"swarph monitor: {exc}", file=sys.stderr)
        return 2
    return 2
