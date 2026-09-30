"""Card #729. Every pass, a cell with nobody reading its inbox is an outage.

One DM per outage. A live watcher clears it, so the next gap can alert again.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

GAP_SECONDS = 60


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
    """The unit leaf monitor status derives from /proc/<pid>/cgroup.

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


def writer_alert(cell: str, cgroup: str | None) -> str:
    who = supervisor_from_cgroup(cgroup)
    if who == "unsupervised":
        return f"{cell}: writer-down, unsupervised"
    return f"{cell}: writer-down, supervised by {who}"


def systemd_owns_monitor(cell: str, run=None) -> bool:
    """True when an enabled swarph-monitor@<cell> unit supervises the writer.

    Lab's monitors are system units. `systemctl --user is-enabled` returns
    not-found (rc 4) for those, while `systemctl is-enabled` returns enabled.
    Defer when either scope is enabled. Hand-start only when both are
    not-enabled or not-found.
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
        systemd_owns_monitor(name, run=run)
        verdict = writer_verdict(reader_alive=True, monitor_rc=rc)
        rec = state.setdefault(name, {})
        if writer_transition(verdict == "writer-down", rec):
            alert.append(name)
    return alert


def _channel_alive(inbox: str, now: float | None = None) -> bool:
    """A channel server writes channel_heartbeat.json beside the inbox every 60 s.

    `swarph channel-serve` takes no inbox argument, so no command line names the
    path; a fresh heartbeat is the only evidence the channel is reading.
    """
    hb = Path(inbox).parent / "channel_heartbeat.json"
    try:
        ts = float(json.loads(hb.read_text(encoding="utf-8")).get("ts") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return (time.time() if now is None else now) - ts < 180


def _watching(inbox: str, cmdlines: Iterable[str]) -> bool:
    """A reader is tail or dm_notify_filter with the inbox path as its own argument,
    or a channel server whose heartbeat is fresh.

    A process that merely mentions the path (a prompt, a log line) is not a reader.
    """
    if _channel_alive(inbox):
        return True
    for cmd in cmdlines:
        parts = cmd.split()
        if inbox not in parts:
            continue
        if any(os.path.basename(p) == "tail" for p in parts):
            return True
        if any(p.endswith("dm_notify_filter") or p.endswith("dm_notify_filter.py") for p in parts):
            return True
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
         timers: Iterable[str] = (), crontab: str = "") -> list[str]:
    """Return the cells that should get the one DM for this outage."""
    alert = []
    timers = list(timers)
    for name, inbox in cells.items():
        rec = state.setdefault(name, {})
        if (_watching(inbox, cmdlines) or _timer_watched(name, timers)
                or _cron_watched(name, crontab)):
            rec["missing_since"] = None
            rec["outage"] = False
            continue
        since = rec.get("missing_since")
        if since is None:
            rec["missing_since"] = now
            continue
        if now - since > gap and not rec.get("outage"):
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


def _cgroup_for(root: Path, cell: str) -> str | None:
    """Cgroup of the monitor pidfile, the same leaf monitor status reads."""
    pidfile = root / cell / "mesh-sidecar" / "monitor.pid"
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except (OSError, ValueError):
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
    alert = scan(cells, cmdlines, state, time.time(), timers=timers, crontab=crontab)
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
        target = escalate or ""
        sent = mesh_send(
            swarph, target, sender, writer_alert(name, _cgroup_for(root, name)),
            token_file)
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
        dm_rc = mesh_send(
            swarph, name, sender, "your DM wake is dead, re-arm", token_file)
        dm_ok = dm_rc == 0
        card = subprocess.run(
            [swarph, "board", "cards", "say", "729", "--as", sender,
             "--to", name, "--content", f"{name}: DM wake is dead, re-arm"],
            check=False)
        esc_ok = True
        if escalate and escalate != name:
            esc_rc = mesh_send(
                swarph, escalate, sender, f"{name}: DM wake is dead, re-arm",
                token_file)
            esc_ok = esc_rc == 0
        if dm_ok and card.returncode == 0 and esc_ok:
            state[name]["outage"] = True
            save_state(state_path, state)
        else:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
