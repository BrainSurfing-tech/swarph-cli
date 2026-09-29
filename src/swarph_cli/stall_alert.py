"""stall_alert — surface a cell whose live session is perpetually busy so its
undelivered DMs don't rot silently. Exponential backoff (6,12,24,48…) prevents
the linear-flood failure (the 145-DM incident, feedback_modal_stalls_cell_wake).

Card #989: the surface is one obligation held by lab-ovh, never a DM to
commander. A second stall tick while that row is open opens nothing new. A
drain closes it. Fail-safe: a failed POST never raises and never blocks
delivery."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from swarph_cli.console_safe import print_safe

_STALL_FIRST = 6  # first alert after this many consecutive deferred ticks
_CARD = 989
_HOLDER = "lab-ovh"
_PREFIX = f"STALL card #{_CARD}"


def is_alert_tick(deferred_ticks: int) -> bool:
    """True exactly at 6, 12, 24, 48, 96 … — first at _STALL_FIRST, doubling."""
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


def _live(gateway: str, token: str) -> list[dict]:
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
        if _PREFIX in (row.get("accept") or "") or _PREFIX in (row.get("what") or ""):
            live.append(row)
    return live


def send_stall_alert(gateway: str, token: str, self_name: str,
                     count: int, pending_n: int) -> bool:
    """Open one obligation on card #989 held by lab-ovh. True when that row
    is open (newly or already). False on any error. Never raises, never
    DMs commander, and a second call while the row is open opens nothing."""
    try:
        gateway = gateway.rstrip("/")
        if _live(gateway, token):
            return True
        _request(
            "POST", f"{gateway}/board/cards/{_CARD}/ask", token,
            {"holder": _HOLDER,
             "what": _what(self_name, count, pending_n),
             "created_by": self_name,
             "kind": "action",
             "fallback_mode": "escalate",
             "fallback_target": _HOLDER,
             "accept": _accept(self_name, count, pending_n)})
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
        live = _live(gateway, token)
        for row in live:
            _request(
                "POST", f"{gateway}/board/obligations/{row['id']}/close", token,
                {"outcome": "pass", "evidence": evidence})
        return True
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print_safe(f"[swarph-daemon] stall-alert close failed: {exc}",
                   file=sys.stderr, flush=True)
        return False
