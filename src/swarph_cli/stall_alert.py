"""stall_alert — surface a cell whose live session is perpetually busy so its
undelivered DMs don't rot silently. Exponential backoff (6,12,24,48…) prevents
the linear-flood failure (the 145-DM incident, feedback_modal_stalls_cell_wake).
Fail-safe: a failed alert POST never raises and never blocks delivery."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

_STALL_FIRST = 6  # first alert after this many consecutive deferred ticks


def is_alert_tick(deferred_ticks: int) -> bool:
    """True exactly at 6, 12, 24, 48, 96 … — first at _STALL_FIRST, doubling."""
    if deferred_ticks < _STALL_FIRST or deferred_ticks % _STALL_FIRST != 0:
        return False
    k = deferred_ticks // _STALL_FIRST
    return (k & (k - 1)) == 0  # k is a power of two


def send_stall_alert(gateway: str, token: str, self_name: str,
                     count: int, pending_n: int) -> bool:
    """POST one commander DM about the stall. True on 2xx; fail-safe False on
    any error (never raises — the daemon must keep draining)."""
    body = json.dumps({
        "from_node": self_name,
        "to_node": "commander",
        "kind": "unblock",
        "content": (f"[stall] {self_name} daemon: {count} consecutive ticks "
                    f"unable to deliver {pending_n} DM(s) into the live session "
                    f"(cell busy/never-idle). Undelivered DMs are queued, not "
                    f"lost; they inject on next idle."),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{gateway}/messages", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[swarph-daemon] stall-alert POST failed: {exc}",
              file=sys.stderr, flush=True)
        return False
