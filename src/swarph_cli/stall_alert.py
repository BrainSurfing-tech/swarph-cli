"""stall_alert — surface a cell whose live session is perpetually busy so its
undelivered DMs don't rot silently. Exponential backoff (60,120,240,480…) prevents
the linear-flood failure (the 145-DM incident, feedback_modal_stalls_cell_wake).
Measured 2026-09-30/10-01: 7 of 7 stall rows fired on busy builder cells that
drained on their own within 7-25 min. The first alert waits 60 ticks (30 min
at the 30s poll) so a long build is not paged at 3 min.

Card #989: the surface is one obligation, never a DM to commander by default.
Card #1135 (commander, 2026-10-14: "route those to culprits or responsible"):
the row NAMES the stalled cell and is held by that cell's RESPONSIBLE peer from
the first alert, so someone who can act is told at tick 60, not at a timeout.
A later timeout is only a second escalation. A second stall tick while the row
is open opens nothing new. A drain closes it. Fail-safe: a failed POST never
raises and never blocks delivery."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from swarph_cli.console_safe import print_safe

_STALL_FIRST = 60  # first alert after this many consecutive deferred ticks
_CARD = 989
_HOLDER = "lab-ovh"
# lab-ovh's own stall cannot escalate to lab-ovh. Another orchestrator holds it.
_OTHER = "drop-on-meta-edge"
# Card #1135. TWO CHOICES ARE UNRULED; each is ONE constant so a ruling flips it
# here and nowhere else (one test each in tests/test_stall_alert.py).
# (1) Who is responsible for a stalled cell. No cell -> responsible-peer source
#     exists (claude_peers.owner is a Meta-Edge USER id, never a cell), so this
#     one peer is responsible for every cell until a ruling names a source.
RESPONSIBLE_FOR_OWNERLESS = "lab-ovh"
# (2) Whether the commander also gets a copy at the first alert. Card #989: a DM
#     to the commander is not a reminder, so the default is no.
COMMANDER_COPY = False
# The second escalation (timeout) lands within a working session, not a day.
_TIMEOUT_HOURS = 2
_PREFIX = f"STALL card #{_CARD}"


def is_alert_tick(deferred_ticks: int) -> bool:
    """True exactly at 60, 120, 240, 480 … — first at _STALL_FIRST, doubling.

    Measured 2026-09-30/10-01: 7 of 7 stall rows fired on busy builder cells
    that drained on their own within 7-25 min. At 6 ticks (3 min on a 30s
    poll) every long build tripped the alert.
    """
    if deferred_ticks < _STALL_FIRST or deferred_ticks % _STALL_FIRST != 0:
        return False
    k = deferred_ticks // _STALL_FIRST
    return (k & (k - 1)) == 0  # k is a power of two


def _accept(self_name: str, count: int, pending_n: int) -> str:
    return (
        f"{_PREFIX} cell={self_name} ticks={count} pending={pending_n} | "
        "PASS = the cell drained and this row is closed | "
        "FAIL = the cell is still unable to deliver"
    )


def _what(self_name: str, count: int, pending_n: int) -> str:
    return (
        f"{_PREFIX}\n"
        f"cell={self_name} ticks={count} pending_dms={pending_n}"
    )


def _open_url(req, timeout):
    host = urllib.parse.urlparse(req.full_url).hostname
    if host in {"127.0.0.1", "localhost", "::1"}:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({})).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with _open_url(req, timeout=10) as resp:
        raw = resp.read().decode()
        if not (200 <= resp.status < 300):
            raise urllib.error.URLError(f"HTTP {resp.status}")
    return json.loads(raw) if raw else {}


def _holder_for(self_name: str) -> str:
    """Who is told at the FIRST alert: the responsible peer.

    There is no cell -> responsible-peer source today. claude_peers.owner is the
    Meta-Edge USER id (a person), not a cell, and must NOT be used: routing a
    stall row to it would page a human. So every cell takes the one constant
    RESPONSIBLE_FOR_OWNERLESS; when a real source exists it plugs in here.
    lab-ovh's own stall goes to the other orchestrator. The stalled cell is
    never its own holder: it cannot act on a notice it cannot receive."""
    if self_name == _HOLDER:
        return _OTHER
    holder = RESPONSIBLE_FOR_OWNERLESS
    return _OTHER if holder == self_name else holder


def _escalation_for(holder: str, self_name: str) -> str:
    """Who the timeout escalates to: a different peer than the holder."""
    if holder == _OTHER:
        return holder  # lab-ovh's own stall: nobody above the other orchestrator
    return _OTHER if holder == _HOLDER else _HOLDER


def pending_count(entries, self_name: str) -> int:
    """How many owed DMs a stall alert should report, WITHOUT the row's own
    notice. Minting the row DMs its holder (gateway /ask inserts one message
    per holder, and a self-ask posts one to the asker's inbox), so when the
    holder is, or later becomes, the stalled cell itself that notice sits in
    the same stuck queue and would inflate the next alert's pending count.
    An entry is the row's own notice when its text carries this alert's prefix
    and names this cell."""
    needle = f"cell={self_name} "
    n = 0
    for e in entries or ():
        text = " ".join(str(v) for v in e.values()) if isinstance(e, dict) else str(e)
        if _PREFIX in text and needle in text:
            continue
        n += 1
    return n


def _cell_row(row: dict, self_name: str) -> bool:
    needle = f"cell={self_name} "
    return needle in (row.get("accept") or "") or needle in (row.get("what") or "")


def _live(gateway: str, token: str, self_name: str) -> list[dict]:
    rows: list[dict] = []
    for status in ("open", "fallback_fired"):
        data = _request(
            "GET",
            f"{gateway}/board/obligations?card_id={_CARD}&status={status}",
            token)
        rows.extend(data.get("obligations") or [])
    seen = set()
    live = []
    for row in rows:
        if row.get("id") in seen:
            continue
        seen.add(row.get("id"))
        if _cell_row(row, self_name):
            live.append(row)
    return live


def send_stall_alert(gateway: str, token: str, self_name: str,
                     count: int, pending_n: int) -> bool:
    """Open one obligation on card #989 held by the responsible peer
    (RESPONSIBLE_FOR_OWNERLESS, lab-ovh today). True when that row is open (newly or
    already). False on any error. Never raises, DMs the commander only when
    COMMANDER_COPY is on, and a second call while the row is open opens
    nothing."""
    try:
        gateway = gateway.rstrip("/")
        if _live(gateway, token, self_name):
            return True
        holder = _holder_for(self_name)
        _request(
            "POST", f"{gateway}/board/cards/{_CARD}/ask", token,
            {"holder": holder,
             "what": _what(self_name, count, pending_n),
             "created_by": self_name,
             "kind": "action",
             "fallback_mode": "escalate",
             "fallback_target": _escalation_for(holder, self_name),
             "timeout_hours": _TIMEOUT_HOURS,
             "accept": _accept(self_name, count, pending_n)})
        if COMMANDER_COPY:
            _request(
                "POST", f"{gateway}/messages", token,
                {"from_node": self_name, "to_node": "commander", "kind": "fyi",
                 "content": _what(self_name, count, pending_n)
                            + f"\nheld by {holder}; escalates to "
                              f"{_escalation_for(holder, self_name)}"})
        return True
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print_safe(f"[swarph-daemon] stall-alert POST failed: {exc}",
                   file=sys.stderr, flush=True)
        return False


def clear_stall_alert(gateway: str, token: str, self_name: str) -> bool:
    """Close the open stall row after a drain. True when none remain open.
    False on any error. Never raises."""
    evidence = (
        f"{_PREFIX} cell={self_name} drained. "
        "The deferred delivery completed, so this row is closed."
    )
    try:
        gateway = gateway.rstrip("/")
        live = _live(gateway, token, self_name)
        for row in live:
            _request(
                "POST", f"{gateway}/board/obligations/{row['id']}/close", token,
                {"outcome": "pass", "evidence": evidence})
        return True
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print_safe(f"[swarph-daemon] stall-alert close failed: {exc}",
                   file=sys.stderr, flush=True)
        return False
