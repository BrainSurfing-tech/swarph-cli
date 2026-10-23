"""stall_alert — surface a cell whose live session is perpetually busy so its
undelivered DMs don't rot silently. Exponential backoff (60,120,240,480…) prevents
the linear-flood failure (the 145-DM incident, feedback_modal_stalls_cell_wake).
Measured 2026-09-30/10-01: 7 of 7 stall rows fired on busy builder cells that
drained on their own within 7-25 min. The first alert waits 60 ticks (30 min
at the 30s poll) so a long build is not paged at 3 min.

Card #989: the surface is one obligation, never a DM to commander by default.
Card #1135 (commander, 2026-10-14 08:30Z, restated in the 2026-10-23 triage):
the row is held by the stalled cell, and the timeout escalates to that cell's
responsible peer. lab-ovh is not the default recipient. lab-ovh's own stall is
the exception: it is held by the other orchestrator. A second stall tick while
the row is open opens nothing new. A drain closes it. Fail-safe: a failed POST
never raises and never blocks delivery."""
from __future__ import annotations

import json
import re
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
# Card #1135. The `responsible:` key in the cell's OWN yaml names who the
# timeout escalates to. This daemon runs inside the stalled cell, so that
# yaml is local. claude_peers.owner is a Meta-Edge USER id, a person, and is
# deliberately never read. An unset, unreadable, or self-naming key does not
# escalate to lab-ovh: lab-ovh is not the default recipient.
RESPONSIBLE_YAML_KEY = "responsible"
RESPONSIBLE_FOR_OWNERLESS = "lab-ovh"  # not a recipient; lab-ovh is not the default
# (2) Whether the commander also gets a copy at the first alert. Card #989: a DM
#     to the commander is not a reminder, so the default is no.
COMMANDER_COPY = False
# The second escalation (timeout) lands within a working session, not a day.
_TIMEOUT_HOURS = 2
# The gateway refuses a holder it does not hold with one of these (#510). ONLY
# these retry with the default: a transient 408/429 (or 400/401/422/5xx) is not
# "the responsible peer is unknown", and routing it to lab-ovh would let the open
# row suppress the re-mint, so the responsible peer would never be told.
_REFUSED_HOLDER_CODES = frozenset({403, 404, 409})
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


def _what(self_name: str, count: int, pending_n: int, note: str = "") -> str:
    return (
        f"{_PREFIX}\n"
        f"cell={self_name} ticks={count} pending_dms={pending_n}"
        + (f"\n{note}" if note else "")
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


_PEER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _responsible_from_yaml(self_name: str) -> str | None:
    """The `responsible:` value in THIS cell's own yaml, or None.

    None in every case that must fall back: no yaml for the cell, the key unset,
    a yaml that cannot be parsed (it may be this cell's, so the cells dir is not
    trusted), two yamls naming the cell with different values, a value that is
    not a mesh peer name, or a value that names the cell itself. Fail-safe: any
    error is None, never an exception into the delivery loop. Reads through
    gh_identity.yaml_claims (one yaml reader)."""
    try:
        from swarph_cli import gh_identity
        claims, unreadable = gh_identity.yaml_claims(
            self_name, key=RESPONSIBLE_YAML_KEY)
    except Exception:  # noqa: BLE001 - PyYAML missing, odd cells dir, anything
        return None
    if unreadable:
        return None
    values = {v.strip() for v, _ in claims if isinstance(v, str) and v.strip()}
    if len(values) != 1:
        return None
    value = values.pop()
    if not _PEER_NAME_RE.fullmatch(value) or value == self_name:
        return None
    return value


def _holder_for(self_name: str) -> str:
    """Who holds the first alert: the stalled cell.

    Its own drain closes the row. lab-ovh is not that holder, including for
    lab-ovh's own stall, which the other orchestrator holds."""
    if self_name == _HOLDER:
        return _OTHER
    return self_name


def _escalation_for(holder: str, self_name: str) -> str | None:
    """The timeout target: the cell's responsible peer, or None.

    None when the key is unset, unreadable, names the holder, or names the
    stalled cell. An absent key does not escalate to lab-ovh. A None here
    omits timeout_hours, and the gateway never sweeps a null timeout_at."""
    responsible = _responsible_from_yaml(self_name)
    if not responsible or responsible in (holder, self_name):
        return None
    return responsible


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


def _ask(gateway: str, token: str, self_name: str, count: int, pending_n: int,
         holder: str, note: str) -> None:
    body = {"holder": holder,
            "what": _what(self_name, count, pending_n, note),
            "created_by": self_name,
            "kind": "action",
            "accept": _accept(self_name, count, pending_n)}
    target = _escalation_for(holder, self_name)
    if target is not None:
        body.update({"fallback_mode": "escalate", "fallback_target": target,
                     "timeout_hours": _TIMEOUT_HOURS})
    # else: no second escalation exists. timeout_hours is OMITTED (null timeout_at
    # is never swept), and so are the fallback fields: the gateway would otherwise
    # store the asker, the stalled cell, as the target.
    _request("POST", f"{gateway}/board/cards/{_CARD}/ask", token, body)


def send_stall_alert(gateway: str, token: str, self_name: str,
                     count: int, pending_n: int) -> bool:
    """Open one obligation on card #989 held by the stalled cell.

    The timeout, when the cell has a responsible peer, escalates to that
    peer. lab-ovh is not the default. True when that row is open (newly or
    already). False on any error, including a holder the gateway refuses:
    that refusal is not rerouted to lab-ovh, so the next tick can try again.
    Never raises. DMs the commander only when COMMANDER_COPY is on. A second
    call while the row is open opens nothing."""
    try:
        gateway = gateway.rstrip("/")
        if _live(gateway, token, self_name):
            return True
        holder = _holder_for(self_name)
        note = ""
        # A refused holder, including _REFUSED_HOLDER_CODES, is not rerouted
        # to lab-ovh. The next alert tick tries this cell again.
        _ask(gateway, token, self_name, count, pending_n, holder, note)
        if COMMANDER_COPY:
            _request(
                "POST", f"{gateway}/messages", token,
                {"from_node": self_name, "to_node": "commander", "kind": "fyi",
                 "content": _what(self_name, count, pending_n, note)
                            + f"\nheld by {holder}; escalates to "
                              f"{_escalation_for(holder, self_name) or 'nobody (no peer above the holder)'}"})
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
