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


def _watching(inbox: str, cmdlines: Iterable[str]) -> bool:
    """A reader is tail or dm_notify_filter with the inbox path as its own argument.

    A process that merely mentions the path (a prompt, a log line) is not a reader.
    """
    for cmd in cmdlines:
        parts = cmd.split()
        if inbox not in parts:
            continue
        if any(os.path.basename(p) == "tail" for p in parts):
            return True
        if any(p.endswith("dm_notify_filter") or p.endswith("dm_notify_filter.py") for p in parts):
            return True
        if any(p.endswith("swarph_cli.channel") or p.endswith("channel.py") for p in parts):
            hb = Path(inbox).parent / "channel_heartbeat.json"
            try:
                data = json.loads(hb.read_text(encoding="utf-8"))
                ts = float(data.get("ts") or 0)
            except (OSError, ValueError, TypeError):
                continue
            if time.time() - ts < 180:
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
        dm = subprocess.run(
            [swarph, "mesh", "send", name, "--as", sender, "--kind", "fyi",
             "--content", "your DM wake is dead, re-arm"],
            check=False)
        card = subprocess.run(
            [swarph, "board", "cards", "say", "729", "--as", sender,
             "--to", name, "--content", f"{name}: DM wake is dead, re-arm"],
            check=False)
        esc_ok = True
        if escalate and escalate != name:
            esc = subprocess.run(
                [swarph, "mesh", "send", escalate, "--as", sender, "--kind", "fyi",
                 "--content", f"{name}: DM wake is dead, re-arm"],
                check=False)
            esc_ok = esc.returncode == 0
        if dm.returncode == 0 and card.returncode == 0 and esc_ok:
            state[name]["outage"] = True
            save_state(state_path, state)
        else:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
