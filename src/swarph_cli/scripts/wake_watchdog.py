"""Card #729. Every pass, a cell with nobody reading its inbox is an outage.

One DM per outage. A live watcher clears it, so the next gap can alert again.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Iterable

GAP_SECONDS = 60


def _watching(inbox: str, cmdlines: Iterable[str]) -> bool:
    for cmd in cmdlines:
        if inbox in cmd and ("tail" in cmd or "dm_notify_filter" in cmd):
            return True
    return False


def _timer_watched(name: str, timers: Iterable[str]) -> bool:
    needle = f"swarph-codex-waker@{name}.timer"
    return any(needle in line for line in timers)


def scan(cells: dict[str, str], cmdlines: Iterable[str], state: dict,
         now: float, *, gap: float = GAP_SECONDS,
         timers: Iterable[str] = ()) -> list[str]:
    """Return the cells that should get the one DM for this outage."""
    alert = []
    timers = list(timers)
    for name, inbox in cells.items():
        rec = state.setdefault(name, {})
        if _watching(inbox, cmdlines) or _timer_watched(name, timers):
            rec["missing_since"] = None
            rec["outage"] = False
            continue
        since = rec.get("missing_since")
        if since is None:
            rec["missing_since"] = now
            continue
        if now - since > gap and not rec.get("outage"):
            rec["outage"] = True
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


def _timer_lines() -> list[str]:
    listed = subprocess.run(
        ["systemctl", "--user", "list-timers", "--all", "--no-legend"],
        capture_output=True, text=True, check=False)
    return listed.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wake_watchdog")
    p.add_argument("--dry-run", action="store_true",
                   help="write state and list unwatched cells; do not send")
    args = p.parse_args(argv)
    root = Path(os.environ.get("SWARPH_STATE_ROOT", os.path.expanduser("~/swarph_state")))
    state_path = Path(os.environ.get(
        "WAKE_WATCHDOG_STATE",
        os.path.expanduser("~/.local/state/wake-watchdog.json")))
    import time
    listed = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=False)
    cmdlines = listed.stdout.splitlines()
    timers = _timer_lines()
    cells = cells_under(root)
    state = load_state(state_path)
    alert = scan(cells, cmdlines, state, time.time(), timers=timers)
    save_state(state_path, state)
    if args.dry_run:
        for name, inbox in cells.items():
            if not _watching(inbox, cmdlines) and not _timer_watched(name, timers):
                print(name, flush=True)
        return 0
    sender = os.environ.get("SWARPH_SELF", "")
    for name in alert:
        print(f"outage {name}", flush=True)
        subprocess.run(
            ["swarph", "mesh", "send", name, "--as", sender, "--kind", "fyi",
             "--content", "your DM wake is dead, re-arm"],
            check=False)
        subprocess.run(
            ["swarph", "board", "cards", "say", "729", "--as", sender,
             "--to", name, "--content", f"{name}: DM wake is dead, re-arm"],
            check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
