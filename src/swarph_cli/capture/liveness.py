"""Actual liveness PROBE for a pinned session's holder (spec §4.4b).

THE root rule (project_pain memory feedback_probe_liveness_not_stale_proxy + spec):
liveness is PROBED, never inferred from a stored proxy. `swarph cell verify` calls
probe_holder_liveness() against the manifest's live_pin_holder so a holder that
CRASHED without clearing its pin (the poison-pin — exactly the failure this primitive
exists to fix) reads as DEAD and the stale pin is cleared, NOT as a permanent block.

Holder = a tmux session name (the cell's tmux_session / role). Live iff the session
exists AND at least one of its pane processes is alive.
"""
from __future__ import annotations

import os
import subprocess
from typing import List, Optional


def _tmux_has_session(holder: str) -> bool:
    try:
        return subprocess.run(
            ["tmux", "has-session", "-t", holder],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _pane_pids(holder: str) -> List[int]:
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-t", holder, "-F", "#{pane_pid}"],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    pids: List[int] = []
    for line in out.stdout.split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def probe_holder_liveness(holder: Optional[str]) -> bool:
    """True iff `holder`'s tmux session exists AND a pane process is alive."""
    if not holder:
        return False
    if not _tmux_has_session(holder):
        return False
    pids = _pane_pids(holder)
    if not pids:
        return False
    return any(_process_alive(pid) for pid in pids)
