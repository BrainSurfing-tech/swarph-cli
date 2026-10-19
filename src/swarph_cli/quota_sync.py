"""Provider usage limits (card #1071, rulings #1403): read each membrane's
own usage display from its pane and write it into the roster's quota fields.

Pure engine, daemon-reachable: no prints, no subprocess, no network here.
``sync_once`` takes every side effect as an injected callable; the thin CLI
wrapper (``swarph roster sync``) wires tmux + HTTP + the state file.

A record is {used_pct|None, credits|None, reset_at|None, blocked:bool,
source:'pane'|'error', as_of} plus, for a codex heads-up bound only,
bound_left_pct. None means unknown — and unknown is never written as 0%
or null, and never clears a prior quota or block.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)

READ_COOLDOWN = timedelta(minutes=5)
BLOCK_HOLD = timedelta(minutes=5)

# Anchored markers from the 10-09 pane survey (card link pane_survey_1009).
# Each parser claims a pane ONLY on its own anchor fragment.
_COPILOT_RE = re.compile(r"Session:\s*([0-9]+(?:\.[0-9]+)?)\s*AIC used")
_CODEX_BOUND_RE = re.compile(
    r"less than\s*([0-9]+(?:\.[0-9]+)?)% of your 5h limit left")
_CODEX_HIT_RE = re.compile(
    r"hit your usage limit\.?\s+try again at\s+(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE)
_LOG_LIMIT_RE = re.compile(
    r"usage limit[^\n]*?try again at\s+(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE)
_RATE_EXCEEDED_RE = re.compile(r"rate limits? exceeded", re.IGNORECASE)
# card #1221: Claude Code /usage, the 2026-10-19 22:42Z capture. The two
# runway windows are "Current session" and "Current week (all models)".
# "Current week (Fable)" is the same weekly reset for one model, so it is
# not a second window. The gateway ranks on used_pct, resets_at and as_of.
_CLAUDE_USAGE_RE = re.compile(
    r"^Current (session|week \(all models\)): "
    r"([0-9]+(?:\.[0-9]+)?)% used \u00b7 resets "
    r"(?:([A-Z][a-z]{2} \d{1,2}), )?(\d{1,2}(?::\d{2})?[ap]m) \(UTC\)\s*$",
    re.MULTILINE,
)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _clock(clock: str) -> Optional[datetime]:
    """`4:59pm` or the on-the-hour form `5pm`."""
    norm = clock[:-2] + clock[-2:].upper()
    try:
        return datetime.strptime(norm, "%I:%M%p" if ":" in norm else "%I%p")
    except ValueError:
        return None


def _claude_reset_iso(date: Optional[str], clock: str, as_of: str) -> Optional[str]:
    """A dated reset uses as_of's year. A bare clock is the next time after as_of."""
    try:
        anchor = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return None
    if anchor.tzinfo is None:
        return None
    anchor = anchor.astimezone(timezone.utc)
    clock_dt = _clock(clock)
    if clock_dt is None:
        return None
    if not date:
        candidate = anchor.replace(
            hour=clock_dt.hour, minute=clock_dt.minute,
            second=0, microsecond=0)
        if candidate <= anchor:
            candidate += timedelta(days=1)
        return candidate.isoformat()
    norm = clock[:-2] + clock[-2:].upper()
    try:
        parsed = datetime.strptime(
            f"{date} {norm} {anchor.year}",
            "%b %d " + ("%I:%M%p" if ":" in norm else "%I%p") + " %Y",
        )
    except ValueError:
        return None
    parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed < anchor - timedelta(days=1):
        parsed = parsed.replace(year=anchor.year + 1)
    return parsed.isoformat()


def _window_rows(text: str, as_of: str) -> list:
    """Session and weekly-all-models lines from a Claude Code /usage display.

    Both lines or neither. One parsed window is the 5h mask card #1217
    exists to stop, so it is dropped and the scalar path is what remains.
    """
    rows = []
    names = []
    for match in _CLAUDE_USAGE_RE.finditer(text or ""):
        reset = _claude_reset_iso(match.group(3), match.group(4), as_of)
        if reset is None:
            continue
        names.append(match.group(1))
        rows.append({"used_pct": float(match.group(2)),
                     "resets_at": reset,
                     "as_of": as_of})
    if len(rows) == 1:
        missing = "weekly" if names[0] == "session" else "session"
        log.warning(
            "quota windows dropped: parsed %s only, %s did not parse; "
            "sending no quota_windows",
            names[0], missing)
        return []
    return rows


def _with_windows(record: Optional[dict], windows: list) -> Optional[dict]:
    if record is None:
        return None
    if windows:
        record = dict(record)
        record["windows"] = windows
    return record


def parse_pane_capture(text: str, as_of: str):
    """One read-only pane capture -> a usage record, or None (unknown).

    Self-selecting: the codex hit-limit line beats its own heads-up bound;
    a grok modal and a mistral rate error are blocked without figures;
    anything else (a Claude cell transcript, agy labels, empty captures)
    is None. A Claude Code /usage display keeps two windows: the session
    and the weekly all-models line (card #1221). The per-model week line
    is not a second runway window.
    """
    text = text or ""
    windows = _window_rows(text, as_of)
    low = text.lower()
    # The mistral provider error surfaces in pane output but is classified
    # source 'error' per ruling 2 — it is an API error, not a usage display.
    if _RATE_EXCEEDED_RE.search(text):
        return _with_windows(
            {"used_pct": None, "credits": None, "reset_at": None,
             "blocked": True, "source": "error", "as_of": as_of}, windows)
    if "buy more credits" in low and "try again" in low:
        return _with_windows(
            {"used_pct": None, "credits": None, "reset_at": None,
             "blocked": True, "source": "pane", "as_of": as_of}, windows)
    hit = _CODEX_HIT_RE.search(text)
    if hit:
        # A trailing period is sentence punctuation, not timestamp.
        reset = hit.group(1).strip().rstrip(".").strip() or None
        return _with_windows(
            {"used_pct": 100, "credits": None, "reset_at": reset,
             "blocked": True, "source": "pane", "as_of": as_of}, windows)
    bound = _CODEX_BOUND_RE.search(text)
    if bound:
        # A bound, not a measurement: used_pct stays null (ruling 2).
        return _with_windows(
            {"used_pct": None, "credits": None, "reset_at": None,
             "blocked": False, "source": "pane", "as_of": as_of,
             "bound_left_pct": float(bound.group(1))}, windows)
    copilot = _COPILOT_RE.search(text)
    if copilot:
        return _with_windows(
            {"used_pct": None, "credits": float(copilot.group(1)),
             "reset_at": None, "blocked": False, "source": "pane",
             "as_of": as_of}, windows)
    if windows:
        return {"used_pct": None, "credits": None, "reset_at": None,
                "blocked": False, "source": "pane", "as_of": as_of,
                "windows": windows}
    return None


def parse_service_log_tail(text: str, as_of: str):
    """A provider log tail (e.g. gpt-service) -> a record, or None.

    Independent of the pane: a limit error here marks the peer blocked
    with the reset the line names — this is the headless-services path.
    """
    text = text or ""
    hit = _LOG_LIMIT_RE.search(text)
    if hit:
        reset = hit.group(1).strip().rstrip(".").strip() or None
        return {"used_pct": 100, "credits": None, "reset_at": reset,
                "blocked": True, "source": "error", "as_of": as_of}
    if _RATE_EXCEEDED_RE.search(text):
        return {"used_pct": None, "credits": None, "reset_at": None,
                "blocked": True, "source": "error", "as_of": as_of}
    return None


def decide_put(prior: Optional[dict], record: Optional[dict],
               now: datetime) -> Optional[dict]:
    """PUT body for one peer, or None when nothing changed.

    prior is the roster row (or None); record is this read (or None for an
    empty/unknown capture, which never writes and never clears). as_of rides
    every PUT. A credits-only read writes nothing — there is no quota field
    for credits, and inventing used_pct is refused by ruling 4.
    """
    if record is None:
        return None
    prior = prior or {}
    as_of = _iso(now)
    body: dict = {}
    used = record.get("used_pct")
    if used is not None and used != prior.get("quota_used_pct"):
        body["quota_used_pct"] = used
    windows = []
    for window in record.get("windows") or []:
        pct = window.get("used_pct")
        reset = window.get("resets_at")
        if pct is None or not reset:
            continue
        windows.append({"used_pct": float(pct),
                        "resets_at": str(reset),
                        "as_of": window.get("as_of") or as_of})
    if windows and windows != (prior.get("quota_windows") or []):
        body["quota_windows"] = windows
    if record.get("blocked"):
        reset = record.get("reset_at")
        until = reset if reset else _iso(now + BLOCK_HOLD)
        if until != prior.get("unavailable_until"):
            body["unavailable_until"] = until
    elif prior.get("unavailable_until"):
        # A read that is not blocked clears a prior block (ruling 1).
        body["unavailable_until"] = None
    if not body:
        return None
    body["quota_as_of"] = as_of
    return body


def _effective_level(record: dict) -> Optional[float]:
    """The percent to threshold against: a measurement, else a bound floor."""
    if record.get("used_pct") is not None:
        return float(record["used_pct"])
    left = record.get("bound_left_pct")
    if left is not None:
        return 100.0 - float(left)
    return None


def check_alerts(peer_state: dict, record: dict):
    """Once-per-crossing alerts: 80, 95, limit. Returns (fired, new_state).

    fired is [(threshold, text)]; thresholds are 80, 95, 'limit'. Dropping
    back below a threshold re-arms it. A missing arm reads armed, so the
    first sight above a threshold fires. A read with no level neither fires
    nor re-arms the percent thresholds (credits are not percents).
    """
    state = {"arm80": peer_state.get("arm80", True),
             "arm95": peer_state.get("arm95", True),
             "arm_limit": peer_state.get("arm_limit", True)}
    fired = []
    level = _effective_level(record)
    if level is not None:
        if level >= 80 and state["arm80"]:
            fired.append((80, f"usage at or past 80% (observed {level:g}%)"))
            state["arm80"] = False
        elif level < 80:
            state["arm80"] = True
        if level >= 95 and state["arm95"]:
            fired.append((95, f"usage at or past 95% (observed {level:g}%)"))
            state["arm95"] = False
        elif level < 95:
            state["arm95"] = True
    if record.get("blocked") or (level is not None and level >= 100):
        if state["arm_limit"]:
            fired.append(("limit", "usage limit reached"))
            state["arm_limit"] = False
    else:
        state["arm_limit"] = True
    return fired, state


def _parse_iso(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def sync_once(*, peers: list, capture: Callable[[str], Optional[str]],
              get_roster: Callable[[], list],
              put_roster: Callable[[str, dict], None],
              send_alert: Callable[[str, str], None],
              service_logs: Optional[dict] = None,
              state: Optional[dict] = None, now: Optional[datetime] = None):
    """One quota pass. Every side effect is injected; returns a summary.

    capture(peer) reads one full-height pane (None when unreadable).
    service_logs maps peer -> log tail text for the headless error path;
    a log hit and a pane read merge with the pane winning ties except a
    log-sourced block, which stands on its own. Peers read within the
    cooldown are skipped. The returned summary carries the state to save.
    """
    now = now or datetime.now(timezone.utc)
    as_of = _iso(now)
    state = state or {}
    peer_states = dict(state.get("peers") or {})
    roster = {r.get("peer"): r for r in (get_roster() or [])
              if isinstance(r, dict)}
    service_logs = service_logs or {}
    summary: dict = {"peers": {}, "as_of": as_of}
    for peer in peers:
        pstate = dict(peer_states.get(peer) or {})
        last = _parse_iso(pstate.get("last_read"))
        if last is not None and (now - last) < READ_COOLDOWN:
            summary["peers"][peer] = {"skipped": "read-cooldown"}
            continue
        record = None
        try:
            pane = capture(peer)
        except Exception:
            pane = None
        if pane:
            record = parse_pane_capture(pane, as_of)
        log_text = service_logs.get(peer)
        if log_text:
            log_rec = parse_service_log_tail(log_text, as_of)
            if log_rec is not None and (record is None
                                        or not record.get("blocked")):
                # The error path is independent: a log block stands even
                # when the pane shows nothing (headless services) or shows
                # a lesser state — blocked wins ties, fail-closed. A pane
                # block already seen keeps its live-TUI record.
                record = log_rec
        entry: dict = {"record": ("unknown" if record is None
                                  else ("blocked" if record.get("blocked")
                                        else "read"))}
        pstate["last_read"] = as_of
        body = decide_put(roster.get(peer), record, now)
        if body is not None:
            put_roster(peer, body)
            entry["put"] = True
        else:
            entry["put"] = False
        if record is not None:
            fired, arms = check_alerts(
                pstate, {"used_pct": record.get("used_pct"),
                         "bound_left_pct": record.get("bound_left_pct"),
                         "blocked": record.get("blocked")})
            pstate.update(arms)
            for threshold, text in fired:
                send_alert(peer, f"quota {threshold}: {text}")
            entry["alerts"] = [t for t, _ in fired]
        else:
            entry["alerts"] = []
        peer_states[peer] = pstate
        summary["peers"][peer] = entry
    summary["state"] = {"peers": peer_states}
    return summary
