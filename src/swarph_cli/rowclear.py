"""Card #432: type ``/clear`` at a row boundary, and follow the new session.

The monitor types ``/clear`` into an opted-in role cell's tmux pane only
when the cell holds no live row, the pane reads idle, and no DM is owed.
``SWARPH_ROWCLEAR`` is ``off`` (default), ``shadow`` (log a would-clear,
type nothing), or ``live``. v1 opt-in is drop-on-meta-edge only: a cell
that owns a project keeps its context (design_1007 / optin_owners).

v1.1 (#1362): a taken row whose step needs are UNMET is waiting, not
working — it does not count as live. Needs state comes from the card's
step graph; anything unreadable about it fails closed (no clear).

A ``/clear`` mints a new Claude session id. The SessionStart hook with
``source=clear`` writes that id into the role's ``<role>.session-id``
pin, so the next spawn resumes the cleared session instead of the long
one the pin still named.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from swarph_cli.cell import (
    CellError,
    _read_session_sidecar,
    _write_session_sidecar,
    session_state_path,
    validate_uuid_str,
)

# v1. Further cells join only by their own or the commander's opt-in.
OPT_IN = frozenset({"drop-on-meta-edge"})

# Gateway statuses are open, closed, and fallback_fired. ``open`` is a
# live row (taken or offered). ``closed`` and ``fallback_fired`` are
# finished and do not block: drop-on-meta-edge holds a fallback_fired
# row that would otherwise make the only opted-in cell never clear.
# Any other status fails closed — a status this code does not know must
# not look like an empty cell.
_LIVE = "open"
_FINISHED = frozenset({"closed", "fallback_fired"})

_MODES = frozenset({"off", "shadow", "live"})


def mode_from_env() -> str:
    raw = os.environ.get("SWARPH_ROWCLEAR", "off").strip().lower()
    return raw if raw in _MODES else "off"


def _latch_path(state) -> "os.PathLike[str]":
    return state.state_dir / "rowclear.json"


def _log_path(state) -> "os.PathLike[str]":
    return state.state_dir / "rowclear.log"


def _read_latch(state) -> Optional[str]:
    path = _latch_path(state)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = data.get("latched_mode") if isinstance(data, dict) else None
    return mode if isinstance(mode, str) else None


def _write_latch(state, mode: Optional[str]) -> None:
    path = _latch_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"latched_mode": mode}) + "\n", encoding="utf-8")


def _tmux_target(state) -> Optional[str]:
    for sink in state.sinks:
        if type(sink).__name__ == "TmuxSink":
            target = getattr(sink, "target", None)
            if target:
                return target
    return None


def _dm_owed(state) -> bool:
    observed = int(state.observed.get("last_msg_id", 0))
    for sink in state.sinks:
        if not getattr(sink, "keeps_ledger", False):
            continue
        led = state.ledger(sink.name)
        if int(led.get("last_delivered_id", 0)) < observed:
            return True
        if led.get("wake_outstanding"):
            return True
    return False


def _step_needs_met(state, row: dict, http_get: Callable,
                      graphs: dict) -> Optional[bool]:
    """Is this open row's work actionable (True), waiting (False), or
    unevaluable (None)?

    A row with no step has no needs to unmeet (ruling_1236: still live).
    Otherwise the card's step graph (GET /board/cards/{id}/graph) names the
    step's needs with a satisfied flag — every need satisfied means the
    holder can act; one unsatisfied need means the cell is between rows.
    Anything unreadable — no card id, a failed graph fetch, a missing step
    entry, a needs entry without a boolean satisfied flag — is None: an
    unevaluable row must not look like an empty cell (fail closed, #1362).
    One graph fetch per card per poll; the same http_get the board read uses.
    """
    step = row.get("step")
    if not step:
        return True
    card_id = row.get("card_id")
    if not isinstance(card_id, int) or isinstance(card_id, bool):
        return None
    if card_id not in graphs:
        url = f"{state.gateway}/board/cards/{card_id}/graph"
        try:
            status, body = http_get(url, state.token)
        except Exception:
            return None
        if status != 200 or not isinstance(body, dict):
            return None
        steps = body.get("steps")
        if not isinstance(steps, list):
            return None
        graphs[card_id] = steps
    for entry in graphs[card_id]:
        if isinstance(entry, dict) and entry.get("step") == step:
            needs = entry.get("needs")
            if not isinstance(needs, list):
                return None
            for need in needs:
                if not isinstance(need, dict) or not isinstance(
                        need.get("satisfied"), bool):
                    return None
                if not need["satisfied"]:
                    return False
            return True
    return None


def _live_row_count(state, http_get: Callable) -> Optional[int]:
    """How many ACTIONABLE rows this cell holds, or None if unread.

    None fails closed: an unreadable board must not look like an empty
    one, or a live row we failed to see gets a ``/clear``.

    v1.1 (#1362): an open row whose step needs are unmet is waiting, not
    working — it does not count. An open row with no step, or whose step
    needs nothing, is still live (ruling_1236 unchanged).
    """
    url = f"{state.gateway}/board/obligations?holder={state.self_name}"
    try:
        status, body = http_get(url, state.token)
    except Exception:
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    rows = body.get("obligations")
    if not isinstance(rows, list):
        return None
    graphs: dict = {}
    live = 0
    for row in rows:
        if not isinstance(row, dict):
            return None
        status = str(row.get("status") or "")
        if status in _FINISHED:
            continue
        if status != _LIVE:
            return None
        met = _step_needs_met(state, row, http_get, graphs)
        if met is None:
            return None
        if met:
            live += 1
    return live


def _append_would_clear(state, *, open_rows: int, pane_state: str) -> None:
    path = _log_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "cell": state.self_name,
        "time": datetime.now(timezone.utc).isoformat(),
        "open_rows": open_rows,
        "pane_state": pane_state,
        "action": "would-clear",
    }
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record) + "\n")


def maybe_rowclear(
    state,
    *,
    agent_running: Callable[[str], Optional[bool]],
    http_get: Callable,
    type_clear: Callable[[str], bool],
) -> str:
    """One poll. Returns the decision, so a test can name why no key went.

    ``off`` and a cell not on the opt-in list return before any read or
    write: they are unchanged. A live row, a busy pane, or an owed DM
    unlatches the idle boundary and types nothing. Unknown rows or an
    unknown pane type nothing and leave the latch alone.
    """
    mode = mode_from_env()
    if mode == "off":
        return "off"
    if state.self_name not in OPT_IN:
        return "not-opted-in"

    live = _live_row_count(state, http_get)
    if live is None:
        return "blocked-rows-unknown"
    if live > 0:
        _write_latch(state, None)
        return "blocked-live-row"

    target = _tmux_target(state)
    if target is None:
        return "blocked-no-pane"
    pane = agent_running(target)
    if pane is True:
        _write_latch(state, None)
        return "blocked-busy"
    if pane is None:
        return "blocked-unknown-pane"
    if _dm_owed(state):
        _write_latch(state, None)
        return "blocked-owed-dm"

    if _read_latch(state) == mode:
        return "latched"

    if mode == "shadow":
        _append_would_clear(state, open_rows=live, pane_state="idle")
        _write_latch(state, mode)
        print(
            f"[rowclear] {state.self_name}: would-clear "
            f"(open_rows={live}, pane=idle) — shadow, zero keys",
            flush=True,
        )
        return "shadow"

    if not type_clear(target):
        print(
            f"[rowclear] {state.self_name}: /clear send failed — "
            "not latched, retried next poll",
            flush=True,
        )
        return "type-failed"
    _write_latch(state, mode)
    print(
        f"[rowclear] {state.self_name}: typed /clear "
        f"(open_rows={live}, pane=idle, no DM owed)",
        flush=True,
    )
    return "live"


def apply_sessionstart_clear(payload: dict[str, Any], role: str) -> bool:
    """Write ``session_id`` into the role pin when SessionStart source is clear.

    Any other source leaves the pin alone: a resume must not clobber the
    id the spawn will attach to. Returns True only when the pin was rewritten.
    """
    if not isinstance(payload, dict) or payload.get("source") != "clear":
        return False
    raw = payload.get("session_id") or payload.get("sessionId") or ""
    try:
        session_id = validate_uuid_str(str(raw))
    except (CellError, TypeError, ValueError):
        return False
    path = session_state_path(role)
    _uuid, recorded_cwd = _read_session_sidecar(path)
    payload_cwd = payload.get("cwd") or ""
    # ruling_1236: a clear from a different project must not rewrite
    # another role's pin (shared HOME, card #964). Refuse only when
    # both sides name a cwd and they differ. A missing side keeps the
    # old fallback.
    if recorded_cwd and str(payload_cwd).strip():
        if os.path.normpath(recorded_cwd) != os.path.normpath(str(payload_cwd)):
            print(
                f"rowclear: payload cwd {payload_cwd} differs from the "
                f"pin's recorded cwd {recorded_cwd} — pin not rewritten",
                file=sys.stderr,
            )
            return False
    cwd = payload_cwd or recorded_cwd or ""
    _write_session_sidecar(path, session_id, cwd)
    return True
