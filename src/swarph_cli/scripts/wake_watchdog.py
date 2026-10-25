"""IMMUNE (cell watchdog), card #729. Every pass, a cell with nobody reading its inbox is an outage.

One alert per outage, then again every REALERT_SECONDS while it stays dead.
A live watcher clears it, so the next gap can alert again.
When the dead cell is this unit's own --as identity, the alert is one
commander-held row, not a DM to the cell (card #1256).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

GAP_SECONDS = 60
WAIT_REARM_GRACE = 600
REALERT_SECONDS = 6 * 60 * 60


def writer_verdict(*, reader_alive: bool, monitor_rc: int) -> str:
    """The heartbeat is the reader. The pull monitor is the writer of inbox.log.

    A live reader with monitor status 2 is writer-down. A live monitor with a
    fresh heartbeat is healthy. This does not start a process.
    """
    if not reader_alive:
        return "reader-down"
    if monitor_rc == 2:
        return "writer-down"
    return "healthy"


def writer_transition(down: bool, rec: dict) -> bool:
    """True only on the edge into writer-down.

    A healthy pass clears the flag. A down pass does not set it: the caller
    marks the alert only after the DM returns 0, so a lost send is retried.
    """
    if not down:
        rec["writer_down"] = False
        return False
    return not bool(rec.get("writer_down"))


def supervisor_from_cgroup(text: str | None) -> str:
    """The unit leaf of a cgroup listing, the same rule monitor status uses.

    A swarph-monitor service, including the non-template names
    swarph-monitor.service and swarph-monitor-<cell>.service, is the
    supervisor. user@<uid>.service is the session manager, not one.
    Anything else is unsupervised.
    """
    if not text:
        return "unsupervised"
    for line in text.splitlines():
        leaf = line.rsplit("/", 1)[-1].strip()
        if not leaf.endswith(".service") or leaf.startswith("user@"):
            continue
        if leaf.startswith("swarph-monitor"):
            return leaf
    return "unsupervised"


def writer_alert(cell: str, recorded: str | None, *, owned: bool = False) -> str:
    """Name the supervisor recorded at start, or the enabled template unit."""
    who = _alert_supervisor(recorded)
    if who == "unsupervised" and owned:
        who = f"swarph-monitor@{cell}.service"
    if who == "unsupervised":
        return f"{cell}: writer-down, unsupervised"
    return f"{cell}: writer-down, supervised by {who}"


def systemd_owns_monitor(cell: str, run=None) -> bool:
    """True when either scope has an enabled swarph-monitor@<cell> unit.

    That result is part of the writer-down alert when the cgroup names no
    swarph-monitor leaf. This function does not start a process.
    """
    unit = f"swarph-monitor@{cell}.service"
    runner = run if run is not None else subprocess.run
    owned = False
    for argv in (
        ["systemctl", "is-enabled", unit],
        ["systemctl", "--user", "is-enabled", unit],
    ):
        try:
            proc = runner(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
        except FileNotFoundError:
            continue
        if proc.returncode == 0:
            owned = True
    return owned


def enforce_writers(cells: dict[str, str], *, status, run=None,
                    state: dict | None = None) -> list[str]:
    """Cells that just transitioned to writer-down.

    Both systemd scopes are still checked. Nothing here starts a monitor.
    Supervision stays with the unit. One name is returned per down edge.
    """
    alert = []
    state = state if state is not None else {}
    for name, inbox in cells.items():
        if not _channel_alive(inbox):
            continue
        try:
            rc = int(status(name))
        except (TypeError, ValueError, OSError):
            continue
        rec_owned = systemd_owns_monitor(name, run=run)
        verdict = writer_verdict(reader_alive=True, monitor_rc=rc)
        rec = state.setdefault(name, {})
        rec["systemd_owned"] = rec_owned
        if writer_transition(verdict == "writer-down", rec):
            alert.append(name)
    return alert


def _channel_alive(inbox: str, now: float | None = None, *,
                   cell: str | None = None, proc_root: Path | None = None) -> bool:
    """A channel server writes channel_heartbeat.json beside the inbox every 60 s.

    `swarph channel-serve` takes no inbox argument, so no command line names the
    path. A fresh timestamp is not enough (card #1256): the recorded pid must
    still be alive, and that pid's parent must be this cell's claude session.
    A reused pid, or a live server whose parent is not the session, is dead.
    """
    hb = Path(inbox).parent / "channel_heartbeat.json"
    try:
        payload = json.loads(hb.read_text(encoding="utf-8"))
        ts = float(payload.get("ts") or 0)
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if (time.time() if now is None else now) - ts >= 180:
        return False
    if proc_root is not None:
        root = proc_root
    else:
        override = os.environ.get("WAKE_WATCHDOG_PROC", "").strip()
        root = Path(override) if override else Path("/proc")
    who = cell or Path(inbox).parent.parent.name
    if not _pid_alive(pid, root):
        return False
    return _parent_is_cell_session(pid, who, root)


def _pid_alive(pid: int, proc_root: Path) -> bool:
    if pid <= 0:
        return False
    try:
        return (proc_root / str(pid)).is_dir()
    except OSError:
        return False


def _read_ppid(pid: int, proc_root: Path) -> int | None:
    try:
        data = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        after = data[data.rfind(")") + 2:].split()
        return int(after[1])
    except (OSError, ValueError, IndexError):
        return None


def _read_cmdline(pid: int, proc_root: Path) -> list[str]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part]


def _flag(argv: list[str], flag: str) -> str | None:
    prefix = flag + "="
    for index, arg in enumerate(argv):
        if arg == flag and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


def _cell_spec(cell: str) -> tuple[str, str, str] | None:
    """(yaml stem, role, session id) for the mesh peer `cell`, or None.

    The spawn name is the file stem (`lab.yaml` → `swarph spawn lab`).
    The role is the claude `--name`. The session id is the pinned resume.
    """
    from swarph_cli.cell import CellError, cells_dir, load_cell

    root = cells_dir()
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*.yaml")):
        try:
            loaded = load_cell(path)
        except (OSError, ValueError, CellError):
            continue
        if getattr(loaded, "name", None) == cell:
            return path.stem, loaded.role or "", loaded.session_id or ""
    return None


def _parent_is_cell_session(pid: int, cell: str, proc_root: Path) -> bool:
    """True when pid's parent is claude for this cell's session.

    A pinned session_id must be the parent's `--resume` or `--session-id`.
    With no pin, `--name` must be the cell's role. Any other parent, including
    a live pid reused by an unrelated process, is not the session.
    """
    ppid = _read_ppid(pid, proc_root)
    if not ppid:
        return False
    argv = _read_cmdline(ppid, proc_root)
    if not argv or Path(argv[0]).name != "claude":
        return False
    spec = _cell_spec(cell)
    if spec is None:
        return False
    _stem, role, session_id = spec
    if session_id:
        return (_flag(argv, "--resume") == session_id
                or _flag(argv, "--session-id") == session_id)
    if role:
        return _flag(argv, "--name") == role
    return False


def _format_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _dead_since(inbox: str, rec: dict) -> str:
    """The heartbeat timestamp, else the moment the watchdog first missed the cell."""
    ts = 0.0
    try:
        raw = json.loads(
            (Path(inbox).parent / "channel_heartbeat.json").read_text(encoding="utf-8")
        ).get("ts")
        ts = float(raw or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        ts = 0.0
    if ts <= 0:
        try:
            ts = float((rec or {}).get("missing_since") or 0)
        except (TypeError, ValueError):
            ts = 0.0
    if ts <= 0:
        return "unknown"
    return _format_ts(ts)


def commander_alert(cell: str, since: str) -> str:
    """The commander-held row. The spawn word is the cell yaml's stem."""
    spec = _cell_spec(cell)
    if spec is None:
        spawn, pane = cell, cell
    else:
        stem, role, _session = spec
        spawn = stem or cell
        pane = role or stem or cell
    return (f"{cell} DM wake dead since {since}; "
            f"fix: /mcp reconnect in the {pane} pane, or swarph spawn {spawn}")


def _clock() -> float:
    raw = os.environ.get("WAKE_WATCHDOG_NOW", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return time.time()


def _waiter_just_delivered(inbox: str, now: float | None = None) -> bool:
    """`swarph mesh wait --once` writes wait_cursor.json beside the inbox when it
    delivers, then exits. The cell re-arms only after acting on the DM, which can
    take minutes; inside that window no waiter is running but the wake is not dead.
    """
    try:
        mtime = (Path(inbox).parent / "wait_cursor.json").stat().st_mtime
    except OSError:
        return False
    age = (time.time() if now is None else now) - mtime
    return 0 <= age < WAIT_REARM_GRACE  # a future mtime is not a delivery


def _watching(inbox: str, cmdlines: Iterable[str], *,
              cell: str | None = None, proc_root: Path | None = None) -> bool:
    """A reader is tail or dm_notify_filter with the inbox path as its own argument,
    a channel server whose heartbeat is fresh and whose parent is the cell's
    claude session, a one-shot `swarph mesh wait` for this cell (running, or
    delivered within WAIT_REARM_GRACE), or the cell's own monitor running with
    a tmux sink.

    A process that merely mentions the path (a prompt, a log line) is not a reader.
    """
    who = cell or Path(inbox).parent.parent.name
    if _channel_alive(inbox, cell=who, proc_root=proc_root) or _waiter_just_delivered(inbox):
        return True
    cell = Path(inbox).parent.parent.name
    for cmd in cmdlines:
        parts = cmd.split()
        if _waits_on(parts, cell):
            return True
        if _monitor_sink_pushes(parts, cell):
            return True
        if inbox not in parts:
            continue
        if any(os.path.basename(p) == "tail" for p in parts):
            return True
        if any(p.endswith("dm_notify_filter") or p.endswith("dm_notify_filter.py") for p in parts):
            return True
    return False


def _monitor_sink_pushes(parts: list[str], cell: str) -> bool:
    """A running `swarph monitor`, started with `--as <cell>` and a tmux sink,
    is a wake path.

    The monitor's tmux sink injects the wake into the cell's pane; card #961
    measured that a cell cannot wake its own session, so this sink is the only
    path that actually STARTS a turn. It names the cell, not the inbox path, so
    it is checked before the inbox-path gate. Observation only: the watchdog
    never starts a monitor. A process that merely quotes the command (a prompt,
    a log line) is not the monitor -- it must BE swarph running the monitor for
    this cell with a tmux deliver sink.
    """
    for i in (0, 1):
        if len(parts) < i + 4 or os.path.basename(parts[i]) != "swarph":
            continue
        if i == 1 and not os.path.basename(parts[0]).startswith("python"):
            continue
        if parts[i + 1:i + 3] != ["monitor", "start"]:
            continue
        rest = parts[i + 3:]
        named = f"--as={cell}" in rest or any(
            a == "--as" and b == cell for a, b in zip(rest, rest[1:]))
        # Any tmux: pane counts, in either form (`--deliver tmux:<pane>` or
        # `--deliver=tmux:<pane>`) -- the sink is the cell's wake, whatever pane
        # it names, so the two spellings must agree.
        has_tmux = any(p.startswith("--deliver=tmux:") for p in rest) or any(
            a == "--deliver" and b.startswith("tmux:")
            for a, b in zip(rest, rest[1:]))
        return named and has_tmux
    return False


def _waits_on(parts: list[str], cell: str) -> bool:
    """`swarph mesh wait --once --as <cell>` names the cell, not the inbox path.

    copilot wakes on this waiter's exit (card #387). The process must BE swarph
    (argv[0], or argv[1] after an interpreter), so a prompt that quotes the
    command is not a reader.
    """
    for i in (0, 1):
        if len(parts) < i + 3 or os.path.basename(parts[i]) != "swarph":
            continue
        if i == 1 and not os.path.basename(parts[0]).startswith("python"):
            continue
        if parts[i + 1:i + 3] != ["mesh", "wait"]:
            continue
        rest = parts[i + 3:]
        named = f"--as={cell}" in rest or any(
            a == "--as" and b == cell for a, b in zip(rest, rest[1:]))
        return "--once" in rest and named
    return False


def _cron_watched(name: str, crontab: str) -> bool:
    """An active crontab line that runs dm_wake.py for this cell is its poller."""
    for line in crontab.splitlines():
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        if name in body and "dm_wake.py" in body:
            return True
    return False


def _timer_watched(name: str, timers: Iterable[str]) -> bool:
    needle = f"swarph-codex-waker@{name}.timer"
    return any(needle in line for line in timers)


def scan(cells: dict[str, str], cmdlines: Iterable[str], state: dict,
         now: float, *, gap: float = GAP_SECONDS,
         timers: Iterable[str] = (), crontab: str = "",
         proc_root: Path | None = None) -> list[str]:
    """Return the cells that should be alerted on this pass.

    The first pass after GAP_SECONDS is the down edge. While the wake stays
    dead, another alert is due every REALERT_SECONDS, measured from alerted_at.
    """
    alert = []
    timers = list(timers)
    for name, inbox in cells.items():
        rec = state.setdefault(name, {})
        if (_watching(inbox, cmdlines, cell=name, proc_root=proc_root)
                or _timer_watched(name, timers)
                or _cron_watched(name, crontab)):
            rec["missing_since"] = None
            rec["outage"] = False
            rec["alerted_at"] = None
            continue
        since = rec.get("missing_since")
        if since is None:
            rec["missing_since"] = now
            continue
        if now - since <= gap:
            continue
        if not rec.get("outage"):
            alert.append(name)
            continue
        try:
            alerted_at = float(rec.get("alerted_at"))
        except (TypeError, ValueError):
            continue
        if now - alerted_at >= REALERT_SECONDS:
            alert.append(name)
    return alert


def cells_under(root: Path) -> dict[str, str]:
    found = {}
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        inbox = child / "mesh-sidecar" / "inbox.log"
        if inbox.is_file():
            found[child.name] = str(inbox)
    return found


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _listing(argv: list[str]) -> str:
    """A missing binary is an empty listing, which is also a host with no timers."""
    try:
        listed = subprocess.run(
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError:
        return ""
    return listed.stdout or ""


def _crontab() -> str:
    return _listing(["crontab", "-l"])


def _timer_lines() -> list[str]:
    return _listing(
        ["systemctl", "--user", "list-timers", "--all", "--no-legend"]).splitlines()


def _alert_supervisor(recorded: str | None) -> str:
    """A unit name recorded at start, or a cgroup blob from an older test.

    A plain unit name is trusted: the monitor wrote it from its own cgroup
    while the pid was still its own. Cgroup text is parsed the same way.
    """
    if not recorded or not str(recorded).strip():
        return "unsupervised"
    text = str(recorded).strip()
    if "/" in text or "\n" in text:
        return supervisor_from_cgroup(text)
    if text.startswith("user@"):
        return "unsupervised"
    return text


def recorded_supervisor(root: Path, cell: str) -> str | None:
    """The supervisor field of monitor.pid.

    The pid in that file may be dead, and a dead pid can be reused by an
    unrelated process. The field was written at start and still answers.
    This function does not open a process listing.
    """
    pidfile = root / cell / "mesh-sidecar" / "monitor.pid"
    try:
        rec = json.loads(pidfile.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    who = rec.get("supervisor")
    if isinstance(who, str) and who.strip():
        return who.strip()
    return None


def mesh_send(swarph: str, to: str, sender: str, content: str,
             token_file: str = "") -> int:
    """Send one FYI. A self-send is refused and logged. The token is a path."""
    if sender and to and sender == to:
        print(f"wake_watchdog: refusing self-send to {to}", file=sys.stderr)
        return 2
    argv = [swarph, "mesh", "send", to, "--as", sender, "--kind", "fyi",
            "--content", content]
    if token_file:
        argv.extend(["--token-file", token_file])
    proc = subprocess.run(argv, check=False)
    return proc.returncode


def _swarph_bin() -> str:
    explicit = os.environ.get("SWARPH_BIN", "")
    if explicit:
        return explicit
    home = os.path.expanduser("~/.local/bin/swarph")
    if os.path.isfile(home):
        return home
    return "swarph"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wake_watchdog")
    p.add_argument("--dry-run", action="store_true",
                   help="write state and list unwatched cells; do not send")
    p.add_argument("--as", dest="sender", default="",
                   help="cell the alert is sent as; the unit sets this")
    p.add_argument("--escalate",
                   default=os.environ.get("WAKE_WATCHDOG_ESCALATE", "drop-on-meta-edge"),
                   help="awake peer who also gets the outage DM (default: drop-on-meta-edge)")
    p.add_argument("--token-file", default="",
                   help="bearer token file for the service identity; the path is passed through, never the value")
    args = p.parse_args(argv)
    root = Path(os.environ.get("SWARPH_STATE_ROOT", os.path.expanduser("~/swarph_state")))
    state_path = Path(os.environ.get(
        "WAKE_WATCHDOG_STATE",
        os.path.expanduser("~/.local/state/wake-watchdog.json")))
    import time
    cmdlines = _listing(["ps", "-eo", "args"]).splitlines()
    timers = _timer_lines()
    crontab = _crontab()
    cells = cells_under(root)
    state = load_state(state_path)
    now = _clock()
    alert = scan(cells, cmdlines, state, now, timers=timers, crontab=crontab)
    sender = args.sender.strip()
    escalate = (args.escalate or "").strip()
    if not args.dry_run and escalate and sender and escalate == sender:
        print("wake_watchdog: escalation peer equals the sender; refusing to send",
              file=sys.stderr)
        return 2
    swarph = _swarph_bin()

    def _status(name: str, _sw: str = swarph) -> int:
        proc = subprocess.run(
            [_sw, "monitor", "status", "--as", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False)
        return proc.returncode

    reported = enforce_writers(cells, status=_status, state=state)
    save_state(state_path, state)
    token_file = (args.token_file or "").strip()
    for name in reported:
        print(f"writer-down {name}", flush=True)
        if args.dry_run or not sender:
            continue
        owned = bool(state.get(name, {}).get("systemd_owned"))
        sent = mesh_send(
            swarph, escalate or "", sender,
            writer_alert(name, recorded_supervisor(root, name), owned=owned),
            "")
        if sent == 0:
            state.setdefault(name, {})["writer_down"] = True
            save_state(state_path, state)
    if args.dry_run:
        for name, inbox in cells.items():
            if not (_watching(inbox, cmdlines) or _timer_watched(name, timers)
                    or _cron_watched(name, crontab)):
                print(name, flush=True)
        return 0
    sender = args.sender.strip()
    if not sender:
        print("wake_watchdog: --as is empty; not marking any cell alerted", file=sys.stderr)
        return 2
    swarph = _swarph_bin()
    rc = 0
    for name in alert:
        print(f"outage {name}", flush=True)
        # Card #1256: the dead cell is this unit's own --as identity. A DM
        # to it, a card post addressed to it, and an escalate to a peer who
        # cannot respawn it all miss the commander. One commander-held row
        # on card 729 shows in the You tab and does not need the cell's wake.
        if name == sender:
            content = commander_alert(name, _dead_since(cells[name], state.get(name) or {}))
            ask_argv = [swarph, "board", "cards", "ask", "729",
                        "--holder", "commander", "--as", sender, content]
            if token_file:
                ask_argv.extend(["--token-file", token_file])
            asked = subprocess.run(ask_argv, check=False)
            if asked.returncode == 0:
                state[name]["outage"] = True
                state[name]["alerted_at"] = now
                save_state(state_path, state)
            else:
                rc = 1
            continue
        dm_ok = mesh_send(
            swarph, name, sender, "IMMUNE (cell watchdog): your DM wake is dead, re-arm", token_file) == 0
        card_argv = [swarph, "board", "cards", "say", "729", "--as", sender,
                     "--to", name, "--content", f"IMMUNE (cell watchdog) — {name}: DM wake is dead, re-arm"]
        if token_file:
            card_argv.extend(["--token-file", token_file])
        card = subprocess.run(card_argv, check=False)
        esc_ok = True
        if escalate and escalate != name:
            esc_rc = mesh_send(
                swarph, escalate, sender, f"IMMUNE (cell watchdog) — {name}: DM wake is dead, re-arm",
                token_file)
            esc_ok = esc_rc == 0
        if dm_ok and card.returncode == 0 and esc_ok:
            state[name]["outage"] = True
            state[name]["alerted_at"] = now
            save_state(state_path, state)
        else:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
