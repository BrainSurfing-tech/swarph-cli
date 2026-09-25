"""Card #729. Every pass, a cell with nobody reading its inbox is an outage.

One DM per outage. A live watcher clears it, so the next gap can alert again.
"""
from __future__ import annotations

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


def scan(cells: dict[str, str], cmdlines: Iterable[str], state: dict,
         now: float, *, gap: float = GAP_SECONDS) -> list[str]:
    """Return the cells that should get the one DM for this outage."""
    alert = []
    for name, inbox in cells.items():
        rec = state.setdefault(name, {})
        if _watching(inbox, cmdlines):
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


def main() -> int:
    root = Path(os.environ.get("SWARPH_STATE_ROOT", os.path.expanduser("~/swarph_state")))
    state_path = Path(os.environ.get(
        "WAKE_WATCHDOG_STATE",
        os.path.expanduser("~/.local/state/wake-watchdog.json")))
    import time
    listed = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=False)
    cmdlines = listed.stdout.splitlines()
    state = load_state(state_path)
    alert = scan(cells_under(root), cmdlines, state, time.time())
    save_state(state_path, state)
    for name in alert:
        print(f"outage {name}", flush=True)
        subprocess.run(
            ["swarph", "mesh", "send", name, "--as", "cursor-lin", "--kind", "fyi",
             "--content", "your DM wake is dead, re-arm"],
            check=False)
        subprocess.run(
            ["swarph", "board", "cards", "say", "729", "--as", "cursor-lin",
             "--to", name, "--content", f"{name}: DM wake is dead, re-arm"],
            check=False)
    return 0
