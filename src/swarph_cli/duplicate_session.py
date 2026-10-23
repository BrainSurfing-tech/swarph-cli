"""duplicate_session — card #513. Detect TWO live sessions under one SWARPH_SELF
on a box and raise ONE alarm row. NEVER kills anything.

Card #419 named the gap on 2026-08-11 ("NO DUPLICATE-SESSION CHECK. Two live
sessions on one pinned identity is not an error state anywhere."); it fired again
2026-08-18 through a different door (#511) and was never carded until #513. The
commander's lean (2026-10-23): detect two live sessions under one SWARPH_SELF and
raise one alarm row; never kill anything; PER-CELL detection in `swarph monitor`,
not the mesh send path.

DESIGN, and its scope stated so nobody later mistakes it for more than it is:
- The live session this cell can count locally, cheaply, from inside its own
  monitor is its own MONITOR process — the process that holds the identity and
  drains the inbox. Another `swarph monitor start --as <cell>` for the SAME cell
  on this box is a second live session under that one SWARPH_SELF.
- Detection is PURE. `duplicate_monitor_pids(entries, cell, my_pid)` walks a
  (pid, argv) listing; the enumeration (`ps -eo pid,args`) and the network are the
  only impure parts, and both are seamed so tests never touch a real process table
  or a real gateway.
- The alarm is ONE board obligation row on card #513, deduped by scanning live
  rows for one naming this cell — the stall_alert shape (card #989): a second tick
  while the row is open opens nothing, and the row CLOSES itself when the
  duplicate is gone. Fail-safe: a failed POST never raises and never blocks the
  delivery loop.
- NEVER KILLS. There is no signal anywhere in this module. #419's ruling on what
  to DO with a duplicate is a human's to apply; this makes the fact visible, it
  does not act on it.

WHAT THIS IS NOT (deliberately, per the card's build notes): it is ATTRIBUTION,
not AUTHENTICATION — two processes with the same token are the same principal,
and this only makes them distinguishable. It does not add a session_id column to
claude_messages and it is not the gateway-side registry; it is the per-cell half
the lean asked for. A duplicate that never spawns a monitor is out of scope here
and is the periodic-sweep half (#513 notes).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from swarph_cli.console_safe import print_safe

# Card #513 — the card that names the gap. The alarm row lands on it.
CARD = 513
# Enumerate at most once every N monitor ticks (~2 min at the 30s default poll).
# A second session is a PERSISTENT fact, not a transient one, so there is no
# backoff: the row is raised once and closed when the duplicate is gone. The
# throttle exists only to bound `ps` spawns.
CHECK_EVERY = 4
_OTHER = "drop-on-meta-edge"  # lab-ovh's own alert goes to the other orchestrator
_PREFIX = f"DUPLICATE-SESSION card #{CARD}"


def _is_swarph_argv(parts: list[str]) -> "int | None":
    """Index of the `swarph` token in argv[0..1], or None.

    Mirrors wake_watchdog._monitor_sink_pushes: argv[0] is `swarph`, or argv[1]
    after a python interpreter (`python .../swarph ...`). A process that merely
    QUOTES the command (a prompt, a log line) is not the process."""
    for i in (0, 1):
        if len(parts) < i + 1 or not parts[i]:
            continue
        if os.path.basename(parts[i]) == "swarph":
            if i == 1 and not os.path.basename(parts[0]).startswith("python"):
                continue
            return i
    return None


def names_cell(parts: list[str], cell: str) -> bool:
    """argv names `cell` via `--as <cell>` or `--as=<cell>` — both spellings."""
    return f"--as={cell}" in parts or any(
        a == "--as" and b == cell for a, b in zip(parts, parts[1:]))


def is_monitor_for(argv, cell: str) -> bool:
    """True iff argv IS this cell's monitor: `swarph monitor start ... --as <cell>`.
    A different cell's monitor, or a non-monitor swarph process, is not. `start`
    is required exactly as wake_watchdog._monitor_sink_pushes requires it; a
    running monitor is always `swarph monitor start`."""
    parts = list(argv) if isinstance(argv, (list, tuple)) else str(argv).split()
    i = _is_swarph_argv(parts)
    if i is None:
        return False
    if parts[i + 1: i + 3] != ["monitor", "start"]:
        return False
    return names_cell(parts[i + 3:], cell)


def duplicate_monitor_pids(entries, cell: str, my_pid: int) -> list[int]:
    """The OTHER live monitor pids for `cell`, sorted. `entries` is an iterable of
    (pid, argv). `my_pid` (this process) is never counted, so a lone monitor
    reports nothing — the alarm must name a SECOND session, not the asker.

    Pure: no process or network calls. Feed it `ps_entries()` in production and a
    literal list in tests."""
    found = set()
    for pid, argv in entries:
        try:
            ipid = int(pid)
        except (TypeError, ValueError):
            continue
        if ipid == my_pid:
            continue
        if is_monitor_for(argv, cell):
            found.add(ipid)
    return sorted(found)


def ps_entries() -> list[tuple[int, list[str]]]:
    """(pid, argv) for every process on the box, via `ps -eo pid,args`. [] on any
    error — an enumeration that cannot run is not a duplicate, and must never
    raise into the monitor loop."""
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,args"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    entries: list[tuple[int, list[str]]] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        pid, _, args = line.partition(" ")
        args = args.strip()
        if pid.isdigit() and args:
            entries.append((int(pid), args.split()))
    return entries


def _what(cell: str, pids: list[int]) -> str:
    return (
        f"{_PREFIX}\n"
        f"cell={cell} duplicates={len(pids)} pids={','.join(str(p) for p in pids)}\n"
        "Two live monitor sessions hold this one identity on this box. Recorded for "
        "a human to act on; NOTHING IS EVER KILLED."
    )


def _accept(cell: str, pids: list[int]) -> str:
    return (
        f"{_PREFIX} cell={cell} pids={','.join(str(p) for p in pids)} | "
        "PASS = the duplicate session is gone (this row closes itself) | "
        "FAIL = two sessions still live"
    )


def _open_url(req, timeout):
    host = urllib.parse.urlparse(req.full_url).hostname
    if host in {"127.0.0.1", "localhost", "::1"}:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({})).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _request(method: str, url: str, token: str, payload: "dict | None" = None) -> dict:
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


def _cell_row(row: dict, cell: str) -> bool:
    needle = f"cell={cell} "
    return needle in (row.get("accept") or "") or needle in (row.get("what") or "")


def _live(gateway: str, token: str, cell: str) -> list[dict]:
    rows: list[dict] = []
    for status in ("open", "fallback_fired"):
        data = _request(
            "GET",
            f"{gateway}/board/obligations?card_id={CARD}&status={status}",
            token)
        rows.extend(data.get("obligations") or [])
    seen = set()
    live = []
    for row in rows:
        if row.get("id") in seen:
            continue
        seen.add(row.get("id"))
        if _cell_row(row, cell):
            live.append(row)
    return live


def _holder_for(cell: str) -> str:
    """Who can receive this alarm. Never the cell itself: it cannot be told
    what it cannot receive (card #513).

    The responsible peer, read through stall_alert's yaml rule, when that
    peer is someone else. Otherwise lab-ovh's own alarm goes to the other
    orchestrator and every other cell's alarm goes to lab-ovh. This is not
    stall_alert._holder_for: card #1135 holds a stall row by the stalled
    cell, and that holder would fail the receive rule here. Any failure
    falls back, never raises."""
    try:
        from swarph_cli import stall_alert
        responsible = stall_alert._responsible_from_yaml(cell)
        if responsible and responsible != cell:
            return responsible
    except Exception:  # noqa: BLE001 - a routing nicety must never break the alarm
        pass
    return _OTHER if cell == "lab-ovh" else "lab-ovh"


def _ask(gateway: str, token: str, cell: str, pids: list[int], holder: str) -> None:
    body = {"holder": holder, "what": _what(cell, pids), "created_by": cell,
            "kind": "action", "accept": _accept(cell, pids)}
    _request("POST", f"{gateway}/board/cards/{CARD}/ask", token, body)


def send_duplicate_alert(gateway: str, token: str, cell: str, pids: list[int]) -> bool:
    """Open ONE obligation row on card #513 held by the responsible peer. True
    when that row is open (newly or already). False on any error. Never raises,
    never kills anything, and a second call while the row is open opens nothing."""
    try:
        gateway = gateway.rstrip("/")
        if _live(gateway, token, cell):
            return True
        _ask(gateway, token, cell, pids, _holder_for(cell))
        return True
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print_safe(f"[swarph-monitor] duplicate-session alert POST failed: {exc}",
                   file=sys.stderr, flush=True)
        return False


def clear_duplicate_alert(gateway: str, token: str, cell: str) -> bool:
    """Close the open duplicate-session row after the duplicate is gone. True when
    none remain open. False on any error. Never raises."""
    evidence = (
        f"{_PREFIX} cell={cell} resolved. Only one live monitor session holds this "
        "identity, so this row is closed."
    )
    try:
        gateway = gateway.rstrip("/")
        for row in _live(gateway, token, cell):
            _request(
                "POST", f"{gateway}/board/obligations/{row['id']}/close", token,
                {"outcome": "pass", "evidence": evidence})
        return True
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print_safe(f"[swarph-monitor] duplicate-session close failed: {exc}",
                   file=sys.stderr, flush=True)
        return False


def check(state, *, entries=None, my_pid: "int | None" = None) -> list[int]:
    """The per-cell check called from the monitor loop.

    Enumerate the live monitors for `state.self_name`, then:
      - >1 (a duplicate): raise ONE deduped alarm row on card #513.
      - exactly 1 (only us): close any open duplicate row (it resolved).
    Returns the duplicate pids found (empty when alone). NEVER kills anything and
    NEVER raises: enumeration and network failures are swallowed to []/False so
    the delivery loop is never interrupted."""
    cell = state.self_name
    if my_pid is None:
        my_pid = os.getpid()
    if entries is None:
        entries = ps_entries()
    dups = duplicate_monitor_pids(entries, cell, my_pid)
    try:
        if dups:
            send_duplicate_alert(state.gateway, state.token, cell, dups)
        else:
            clear_duplicate_alert(state.gateway, state.token, cell)
    except Exception as exc:  # noqa: BLE001 - never break the deliver loop
        print_safe(f"[swarph-monitor] duplicate-session check error: {exc}",
                   file=sys.stderr, flush=True)
    return dups
