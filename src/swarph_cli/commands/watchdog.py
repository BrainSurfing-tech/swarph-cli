"""``swarph watchdog`` — Phase 7 / v0.7 stranded-session detection + recovery.

Designed as a cron-callable one-shot check that detects when a Claude
session has gone dark (API throttle / harness death / inbox-watcher
broken) AND has unprocessed DMs queued at the mesh-gateway, then
attempts a two-stage recovery:

* **A1 (cheap re-engage)** — `tmux send-keys` injects a wake-prompt
  into the interactive tmux session's input buffer. If the session is
  alive-but-throttled (today's failure mode), the prompt queues and
  processes on resume. If the session is dead, A1 is a no-op (no harm).
  Repeated up to N times with backoff.

* **A2 (full respawn)** — If A1 escalations exhaust without the cursor
  advancing, the original session is presumed dead. Watchdog kills the
  stale tmux session and respawns via `swarph spawn <role>` (which
  resumes the same session-id from sidecar — the dead session's UUID
  is reused, so the fresh sibling takes over the same /resume picker
  slot).

Per drop-mother review #1021 design:

* **Detection**: cursor file mtime PRIMARY + pgrep claude FALLBACK,
  AND-gate (both must be dark to fire — avoids false-positive on
  legitimate-pause noise).
* **Threshold**: 30min default — comfortably above legitimate-pause
  noise floor + comfortably below typical-stranded-session duration.
* **Production blast radius**: LOW-MODERATE. Cursor file persists
  DM-processed state across respawns (filters already-processed DMs).
  v0 does NOT yet pass `--respawn-after-time` to fresh sibling
  (queued as `swarph spawn --respawn-after-time` enhancement; v0.8+
  scope per mother #1021).

Per beta #1011 (5 primitives): this watchdog implements primitive (3)
throttle-detection + auto-respawn. Primitive (1) mesh-DM queue is
already shipping and provides the queued-context that fresh siblings
inherit on respawn.

**Per-slot cron coverage at v0.7** (beta iter-1 #1029): each
``swarph watchdog --check --cell <role>`` invocation is single-cell
scope. With v0.7 PR-B auto-suffix in play, sibling slots
(``<role>-2``, ``<role>-3``, etc.) need their own cron entries:

  */5 * * * * swarph watchdog --check --cell drop-on-meta-edge
  */5 * * * * swarph watchdog --check --cell drop-on-meta-edge-2

v0.8+ multi-cell watchdog will walk sidecars matching
``<base_role>{,-N}.session-id`` and check each per-invocation, reducing
operator-cron surface to one entry per base-role. Tracked in v0.8+
candidate queue alongside ``--respawn-after-time``, per-cell-yaml
threshold tuning, mesh-down detection, and ``swarph cleanup-sessions``
(beta #987).

Substrate-doc R7 §11.1.7 lineage: this is operator-tooling sub-layer,
not a substrate primitive. The substrate primitive that would
*prevent* the stranding (S-G spawn-context endpoint with liveness
heartbeats) is v0.8+ scope. v0.7 watchdog is the operator-discipline
gap-filler that makes the substrate-evidence event today (3hr
throttle, 6+ stranded DMs, recovered on commander manual chime)
recoverable without commander intervention next time.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


_DEFAULT_THRESHOLD_SEC = 1800  # 30 minutes
_DEFAULT_A1_RETRIES = 3
_DEFAULT_A1_BACKOFF_SEC = 60
_DEFAULT_GATEWAY_URL = "http://localhost:8788"
# F3 — tmux pane_activity gate threshold. If pane has activity within this
# many seconds, suppress A1 (session is working, not stalled). 600s (10min)
# is comfortably above legitimate-pause noise + comfortably below the
# 30min cursor-staleness threshold, so the two gates compose cleanly.
_DEFAULT_PANE_ACTIVITY_THRESHOLD_SEC = 600
# Phase 4 (v0.7.6) — peer-health-event poll defaults. The recovery
# event we care about is `usage_limit_reset` (throttle cleared; session
# may be sitting idle unaware of queued DMs). 600s window catches a
# reset that fired up to 10min before this cron tick. 120s recovery
# threshold gives the session a brief grace period to notice the reset
# itself before we send-keys at it.
_DEFAULT_PEER_HEALTH_WINDOW_SEC = 600
_DEFAULT_PEER_HEALTH_RECOVERY_THRESHOLD_SEC = 120
_RECOVERY_EVENT_TYPES = ("usage_limit_reset",)

_USAGE = """\
Usage:
  swarph watchdog --check [--cell ROLE] [--cursor PATH] [--threshold SEC]
                          [--gateway URL] [--tmux-session NAME]
                          [--peer NAME] [--no-respawn]
                          [--peer-health-poll] [--peer-health-window-sec SEC]
                          [--peer-health-recovery-threshold SEC]
                          [--log PATH] [--verbose]
  swarph watchdog --install-service [--cell ROLE] [--dry-run]

Detects stranded Claude sessions (API throttle / harness death) and attempts
recovery via tmux send-keys A1 wake-prompt, escalating to swarph spawn
respawn (A2) on persistent darkness.

Designed for cron invocation:
  */5 * * * * swarph watchdog --check --cell lab >> ~/.local/log/swarph-watchdog.log 2>&1

OR systemd timer (v0.7.3+, closes ev_6954f748 substrate-component-installation-gap):
  sudo swarph watchdog --install-service [--cell <role>]
  # → installs /etc/systemd/system/swarph-watchdog.{service,timer}
  # → installs /etc/default/swarph-watchdog with SWARPH_CELL=<role>
  # → daemon-reload + enable --now swarph-watchdog.timer

Detection (mother #1021 AND-gate design):
  PRIMARY:  cursor file mtime — most-recent Claude action (drain script touches it)
  FALLBACK: pgrep claude on tmux session — confirms process aliveness
  AND-gate: both signals must indicate dark beyond --threshold to fire

Recovery escalation (beta #1019 two-stage):
  A1: tmux send-keys wake-prompt; queues in input buffer; processes on resume
  A2: After N×A1 fails (cursor still stale): kill stale tmux session +
      `swarph spawn <role>` to respawn (resumes same session-id from sidecar)

Flags:
  --check              one-shot check (cron-callable; exits with status code)
  --cell ROLE          cell-yaml role; defaults to current $SWARPH_CELL or 'lab'
  --cursor PATH        cursor JSON path; default $TMPDIR/<role>-cursor.json
                       fallback /tmp/lab-claude-cursor.json
  --threshold SEC      darkness threshold; default 1800 (30 min)
  --gateway URL        mesh-gateway URL for unread-DM check; default localhost:8788
  --tmux-session NAME  tmux session name; default = cell role
  --peer NAME          mesh peer name for unread-DM query; default = cell name
  --no-respawn         A1 only; don't escalate to A2 (dry-run mode)
  --peer-health-poll                    Phase 4: also query /peer-health-events.
                                        On recent usage_limit_reset event, treat
                                        sessions as wake-candidates even before
                                        the 30min cursor-staleness threshold.
                                        Requires MESH_GATEWAY_TOKEN in env.
  --peer-health-window-sec SEC          how far back to look for recovery
                                        events; default 600 (10 min)
  --peer-health-recovery-threshold SEC  min cursor staleness before a recovery
                                        event promotes the session to wake-
                                        candidate; default 120 (2 min). Avoids
                                        poking a session that JUST got reset
                                        and is already self-recovering.
  --log PATH           append diagnostic log; default $XDG_STATE_HOME/swarph/watchdog.log
  --verbose            also write diagnostics to stderr

Exit codes:
  0  no action taken (session healthy or no unread DMs queued); install ok
  1  A1 fired (wake-prompt sent)
  2  A2 fired (full respawn triggered)
  3  detection error (cursor unreadable / gateway unreachable)
  4  configuration error (invalid args, no cell.yaml resolved); install needs sudo
  5  install error (file write failed / systemctl failed)
"""


def _now() -> int:
    return int(time.time())


def _stat_mtime(path: Path) -> Optional[int]:
    try:
        return int(path.stat().st_mtime)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _resolve_cursor_path(
    role: str,
    explicit: Optional[str],
    cell_yaml_value: Optional[str] = None,
) -> Path:
    """Resolve cursor file path with documented fallback chain.

    Precedence (F4 — mother #1057/#1060 + beta #1061/#1065):
      1. Explicit ``--cursor`` CLI arg (highest)
      2. ``cell.yaml`` extra.cursor_path when --cell present
      3. ``$TMPDIR/<role>-cursor.json``
      4. ``/tmp/lab-claude-cursor.json`` (legacy lab-orchestrator default)

    F4 closes the host-prefix-variant + sibling-instance-variant gap
    class — cell.yaml carries the canonical cursor path per-cell, watchdog
    auto-resolves when --cell is provided. Eliminates the silent-default-
    to-lab-prefix failure mode that gave droplet 23hr of cursor-unreadable
    errors before catch.
    """
    if explicit:
        return Path(explicit).expanduser()
    if cell_yaml_value:
        return Path(cell_yaml_value).expanduser()
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    primary = Path(tmpdir) / f"{role}-cursor.json"
    if primary.exists():
        return primary
    # lab-orchestrator's documented cursor path per session_start_reminder.txt
    return Path("/tmp/lab-claude-cursor.json")


def _resolve_tmux_session(
    role: str,
    explicit: Optional[str],
    cell_yaml_value: Optional[str] = None,
) -> str:
    """Resolve tmux session name with documented fallback chain.

    Precedence (F4 sibling to cursor_path):
      1. Explicit ``--tmux-session`` CLI arg
      2. ``cell.yaml`` extra.tmux_session when --cell present
      3. Role itself (convention default)

    Mother's sibling-instance variant (#1061): when slot-N siblings spawn,
    each slot needs its own tmux session name; the cell.yaml that pins the
    slot SHOULD also pin the tmux_session to keep the watchdog's reads
    consistent with the spawn's writes.
    """
    if explicit:
        return explicit
    if cell_yaml_value:
        return cell_yaml_value
    return role


def _read_cell_yaml_pins(role: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort read of cell.yaml extra.cursor_path + extra.tmux_session.

    Tries the cwd-local ``./cell.yaml`` first (matches hook_output discovery),
    falls back to ``<cells_dir>/<role>.yaml``. Returns (None, None) on any
    failure — F4 is additive non-breaking, malformed cell.yaml falls through
    to the legacy convention defaults.

    NOTE: ``cursor_path`` / ``tmux_session`` live in ``Cell.extra`` (forward-
    compat catch-all per swarph-shared v0.3) in v0.7.2. swarph-shared 0.4
    will graduate them to first-class typed fields on ``Cell``; this reader
    will continue to work because graduate-to-typed-field preserves the
    extra-dict reading path (per swarph-shared's documented forward-compat
    discipline).
    """
    from swarph_cli.cell import (
        cells_dir,
        discover_cell_in_cwd,
        load_cell,
        CellError,
    )

    cell_path = discover_cell_in_cwd()
    if cell_path is None:
        candidate = cells_dir() / f"{role}.yaml"
        if candidate.is_file():
            cell_path = candidate
    if cell_path is None:
        return None, None

    try:
        cell = load_cell(cell_path)
    except (CellError, OSError):
        return None, None

    extra = cell.extra or {}
    cursor_path = extra.get("cursor_path")
    tmux_session = extra.get("tmux_session")
    return (
        str(cursor_path) if cursor_path else None,
        str(tmux_session) if tmux_session else None,
    )


def _resolve_log_path(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    state_root = os.environ.get("XDG_STATE_HOME", "").strip()
    if state_root:
        return Path(state_root) / "swarph" / "watchdog.log"
    return Path.home() / ".local" / "state" / "swarph" / "watchdog.log"


def _gateway_unread_count(gateway: str, peer: str, token: Optional[str]) -> Optional[int]:
    """Query gateway for unread DM count addressed to peer.

    Returns int count on success; None on any failure (treat as "don't
    know — assume unread" so watchdog still tries to wake).
    """
    url = f"{gateway.rstrip('/')}/messages?to_node={peer}&unread_only=true&limit=1"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None
    # Gateway shape varies — handle both list and {"messages": [...]} forms
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return len(data["messages"])
    return None


def _gateway_recent_recovery_event(
    gateway: str,
    peer: str,
    window_sec: int,
    token: Optional[str],
) -> Optional[dict]:
    """Phase 4 (v0.7.6) — query /peer-health-events for a recent recovery event.

    Returns the most recent event whose ``event_type`` is in
    ``_RECOVERY_EVENT_TYPES`` (currently just ``usage_limit_reset``) for
    this peer within the last ``window_sec`` seconds. Returns None if no
    such event exists OR if the query fails (treat absence + error as
    "no override"; the regular cursor-staleness path still applies).

    Why this matters: the lab + drop both hit ``usage_limit_reset`` from
    Claude's quota system — the throttle clears, but the session has no
    autonomous mechanism to notice. DMs queued during the throttle sit
    unread until commander manually chimes the session, OR until the
    30min cursor-staleness threshold trips A1. Phase 4 closes that gap
    by lowering the threshold to ``--peer-health-recovery-threshold``
    (default 2min) once the gateway sees the reset event.

    Detection ≠ recovery distinction: the gateway already CAPTURES these
    events (claude_session_event_logger.py + POST /peer-health-events).
    What was missing was the wake-up mechanism — this function plus the
    fall-through in run_check is the watchdog half of the loop.
    """
    since_dt = datetime.now(timezone.utc) - timedelta(seconds=window_sec)
    since_iso = since_dt.isoformat()
    query = urllib.parse.urlencode(
        {"peer": peer, "since": since_iso, "limit": 50},
    )
    url = f"{gateway.rstrip('/')}/peer-health-events?{query}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return None
    # Server sorts by time DESC, so the first match is the most recent.
    for ev in events:
        if isinstance(ev, dict) and ev.get("event_type") in _RECOVERY_EVENT_TYPES:
            return ev
    return None


def _process_alive(tmux_session: str) -> bool:
    """Detect if a claude process is running inside the tmux session.

    Returns True iff there's at least one ``claude`` process whose
    parent is the named tmux session. Best-effort; uses pgrep+ps; falls
    back to True (assume alive) if detection itself errors so we don't
    fire A2 on detection-broken-system.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False  # no claude process anywhere
        # At least one claude found — return True.
        # We don't enforce tmux-parentage here since pgrep matching by
        # tmux-pane-process is fragile across tmux versions; "any claude"
        # is sufficient for FALLBACK signal per mother #1021 AND-gate
        # design (cursor staleness is the PRIMARY).
        return bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True  # assume alive on detection error


def _tmux_session_exists(name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _tmux_send_keys(name: str, text: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", name, text, "Enter"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _pane_activity_age_sec(name: str) -> Optional[int]:
    """Age in seconds since the tmux pane's last activity event.

    Reads tmux's `#{pane_activity}` format variable, which returns a unix
    epoch timestamp of the most recent activity in the active pane of the
    target session. Returns None if tmux is missing, the session doesn't
    exist, or tmux's output isn't parseable as an integer epoch.

    Used by F3 (mother #1087 / drop-on-meta-edge proposal) as a third
    AND-gate input to distinguish (a) session genuinely stalled from (b)
    session actively working in a long bash block. cursor-mtime alone
    measures "time since last turn-end" not "time since last activity";
    pane_activity covers the mid-turn-active case.

    Returns None on detection error so the caller can fall through to
    the legacy AND-gate behavior — F3 is a strengthening of the gate,
    not a replacement of it.
    """
    try:
        result = subprocess.run(
            ["tmux", "display", "-p", "-t", name, "#{pane_activity}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        out = result.stdout.strip()
        if not out:
            return None
        return max(0, _now() - int(out))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None


def _tmux_kill_session(name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _spawn_via_swarph(role: str, tmux_session: str) -> bool:
    """A2 escalation: kill stale tmux + respawn via `swarph spawn`.

    Reuses sidecar UUID (R5 fix) so fresh sibling takes over the same
    /resume picker slot. Cell-yaml-driven cwd, starter prompt, etc.
    """
    if _tmux_session_exists(tmux_session):
        _tmux_kill_session(tmux_session)

    swarph_bin = os.environ.get("SWARPH_BIN", "swarph")
    spawn_cmd = (
        f"tmux new -d -s {shlex.quote(tmux_session)} "
        f"{shlex.quote(swarph_bin)} spawn {shlex.quote(role)}"
    )
    try:
        result = subprocess.run(
            spawn_cmd, shell=True,
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _a1_marker_path(log_path: Path, role: str, tmux_session: Optional[str] = None) -> Path:
    """Marker file recording the cursor_mtime at which A1 was last fired.

    Co-located with the watchdog log so it inherits the same XDG_STATE_HOME
    discipline. Cleared on cursor-advance OR A2 escalation. Used to suppress
    repeat A1 fires within the same stale window — fix for the spam incident
    where cron fired A1 every 5min for 65min into an active session's tmux
    input buffer (commander #1092 + droplet #1087).

    Keyed on ``(role, tmux_session)`` post-F4 so sibling-instance patterns
    (alpha+beta drop-on-meta-edge per project_drop_mitosis_to_meta_edge)
    don't clobber each other's markers — mother's flag from #1103 closed in
    v0.7.2. tmux_session is sanitized to alphanumeric + ``-_.`` for the
    filename to avoid path-traversal or weird characters from cell.yaml-
    pinned values.

    NOTE (mother #1138 sanitization edge case): two siblings whose
    ``tmux_session`` values differ ONLY in disallowed characters (e.g.,
    ``cell:a`` vs ``cell:b`` — colons sanitized to ``_`` collapsing both
    to ``cell_a`` / ``cell_b`` — fine in this example, but ``cell:a`` vs
    ``cell$a`` would both collapse to ``cell_a``) would collide post-
    sanitization. cell.yaml-pinned ``tmux_session`` values SHOULD differ
    in alphanumeric content, not just punctuation. Cosmetic in practice
    (operators don't choose session names that close), but worth knowing.
    """
    safe_tmux = "".join(
        c if (c.isalnum() or c in "-_.") else "_"
        for c in (tmux_session or role)
    )
    return log_path.parent / f"a1-fired-{role}-{safe_tmux}.marker"


def _a1_already_fired_at(marker: Path, cursor_mtime: int) -> bool:
    """Returns True if a previous A1 was fired with this exact cursor_mtime.

    Same cursor_mtime ⇒ no cursor advance since last fire ⇒ we're still in
    the same stale window ⇒ another A1 would spam. Suppresses the fire.
    """
    try:
        return int(marker.read_text().strip()) == cursor_mtime
    except (FileNotFoundError, OSError, ValueError):
        return False


def _record_a1_fired(marker: Path, cursor_mtime: int) -> None:
    """Best-effort marker write; failures are logged elsewhere but never block."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(cursor_mtime))
    except OSError:
        pass


def _clear_a1_marker(marker: Path) -> None:
    """Idempotent marker removal. Called on A2 escalation paths."""
    try:
        marker.unlink()
    except (FileNotFoundError, OSError):
        pass


def _log_event(log_path: Path, event: str, details: dict, verbose: bool = False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _now(),
        "event": event,
        "details": details,
    }
    line = json.dumps(entry, sort_keys=True) + "\n"
    try:
        with log_path.open("a", encoding="utf-8") as fp:
            fp.write(line)
    except OSError as exc:
        # Logging failure shouldn't block the watchdog itself
        print(f"swarph watchdog: log write failed: {exc}", file=sys.stderr)
    if verbose:
        print(line, file=sys.stderr, end="")


def run_check(args: argparse.Namespace) -> int:
    role = args.cell
    # F4 — cell.yaml-pinned cursor_path + tmux_session (mother #1057/#1060
    # + beta #1061/#1065). Reads cell.yaml `extra.cursor_path` /
    # `extra.tmux_session` when --cell is provided; explicit CLI args still
    # win. Best-effort: malformed cell.yaml falls through to legacy
    # convention defaults (additive non-breaking).
    cell_cursor, cell_tmux = _read_cell_yaml_pins(role)
    cursor = _resolve_cursor_path(role, args.cursor, cell_cursor)
    tmux_session = _resolve_tmux_session(role, args.tmux_session, cell_tmux)
    log_path = _resolve_log_path(args.log)
    threshold = args.threshold
    pane_activity_threshold = args.pane_activity_threshold
    peer = args.peer or role
    gateway = args.gateway
    token = os.environ.get("MESH_GATEWAY_TOKEN")
    verbose = args.verbose

    diag = {
        "role": role,
        "cursor": str(cursor),
        "threshold_sec": threshold,
        "tmux_session": tmux_session,
        "peer": peer,
        "pane_activity_threshold_sec": pane_activity_threshold,
        "cell_yaml_pinned_cursor": cell_cursor is not None,
        "cell_yaml_pinned_tmux": cell_tmux is not None,
    }

    # PRIMARY signal: cursor file mtime
    cursor_mtime = _stat_mtime(cursor)
    if cursor_mtime is None:
        diag["error"] = f"cursor file unreadable: {cursor}"
        _log_event(log_path, "error", diag, verbose)
        return 3
    cursor_age = _now() - cursor_mtime
    diag["cursor_age_sec"] = cursor_age

    if cursor_age <= threshold:
        # Phase 4 (v0.7.6) — peer-health-event override. If the gateway
        # observed a recent recovery event (usage_limit_reset) for this
        # peer AND the cursor is at least somewhat stale, fall through
        # to the A1 path so an idle-after-reset session gets nudged.
        # When --peer-health-poll is OFF, behavior is identical to v0.7.5.
        if args.peer_health_poll:
            recovery_event = _gateway_recent_recovery_event(
                gateway, peer, args.peer_health_window_sec, token,
            )
            diag["peer_health_poll"] = True
            diag["recovery_event_seen"] = bool(recovery_event)
            if recovery_event:
                diag["recovery_event_type"] = recovery_event.get("event_type")
                diag["recovery_event_time"] = recovery_event.get("time")
            if recovery_event and cursor_age > args.peer_health_recovery_threshold:
                # Promote to wake-candidate. Don't return — fall through
                # below to the existing process_alive / unread / F1-F3
                # gates, which still get a vote. This is a threshold
                # override, not a gate bypass.
                diag["phase4_override"] = "fall_through_to_a1"
            else:
                # Either no recovery event, OR cursor is fresh enough
                # that the session is likely self-recovering. No action.
                diag["decision"] = (
                    "healthy_cursor_fresh_recovery_too_recent"
                    if recovery_event
                    else "healthy_cursor_fresh"
                )
                _log_event(log_path, "noop", diag, verbose)
                return 0
        else:
            # Cursor recent — Claude has been active. No action.
            diag["decision"] = "healthy_cursor_fresh"
            _log_event(log_path, "noop", diag, verbose)
            return 0

    # FALLBACK signal: pgrep claude (per mother #1021 AND-gate)
    process_alive = _process_alive(tmux_session)
    diag["process_alive"] = process_alive

    # Check unread DM queue at gateway. If no unread DMs, no need to wake.
    unread = _gateway_unread_count(gateway, peer, token)
    diag["unread_count"] = unread

    # Decision matrix (post commander #1092 + droplet #1087 + #1089 hardening):
    # cursor_stale + not process_alive            → A2 (dead, respawn regardless of unread)
    # cursor_stale + process_alive + unread > 0   → A1 (alive but throttled, prompt may unblock)
    # cursor_stale + process_alive + unread = 0   → noop (no DMs to drain anyway)
    # cursor_stale + process_alive + unread None  → noop (F2 fail-closed: can't verify work, don't poke)
    # cursor_stale + a1_marker matches cursor_mtime → noop (F1 same-window suppression)

    marker = _a1_marker_path(log_path, role, tmux_session)
    diag["a1_marker"] = str(marker)

    if not process_alive:
        # A2 escalation — clear the A1 marker so the next A1 (after respawn) fires.
        _clear_a1_marker(marker)
        diag["decision"] = "a2_respawn_process_dead"
        if args.no_respawn:
            diag["dry_run_skip"] = True
            _log_event(log_path, "a2_dry_run", diag, verbose)
            return 2
        spawn_ok = _spawn_via_swarph(role, tmux_session)
        diag["spawn_ok"] = spawn_ok
        _log_event(log_path, "a2_respawn", diag, verbose)
        return 2 if spawn_ok else 4

    # Process is alive but cursor is stale.
    # F2 — fail-closed when unread can't be verified. Trade false-negative for
    # false-positive ("respect peer-time when uncertain" per droplet #1089).
    # Production incident shape (commander #1092): gateway returned None for
    # unread; old code fell through to A1, spamming the tmux buffer for 65min.
    if unread is None:
        diag["decision"] = "noop_unread_unknown"
        _log_event(log_path, "noop", diag, verbose)
        return 0

    if unread == 0:
        diag["decision"] = "noop_no_unread"
        _log_event(log_path, "noop", diag, verbose)
        return 0

    if not _tmux_session_exists(tmux_session):
        # Process alive somewhere but tmux session gone — partial state.
        # Treat as A2 case: respawn fresh sibling.
        _clear_a1_marker(marker)
        diag["tmux_missing"] = True
        diag["decision"] = "a2_respawn_tmux_missing"
        if args.no_respawn:
            _log_event(log_path, "a2_dry_run", diag, verbose)
            return 2
        spawn_ok = _spawn_via_swarph(role, tmux_session)
        diag["spawn_ok"] = spawn_ok
        _log_event(log_path, "a2_respawn", diag, verbose)
        return 2 if spawn_ok else 4

    # F1 — same-stale-window suppression. If A1 already fired at this exact
    # cursor_mtime, further A1s would only stack wake-prompts in the tmux
    # input buffer (commander #1092: 13 fires across 65min on a session that
    # was actively working but cursor only updates at turn-end). Fire AT MOST
    # ONCE per stale window; re-arm only when cursor advances (recovery) or
    # A2 escalates (respawn clears the marker above).
    if _a1_already_fired_at(marker, cursor_mtime):
        diag["decision"] = "noop_a1_already_fired_this_window"
        _log_event(log_path, "noop", diag, verbose)
        return 0

    # F3 — tmux pane_activity AND-gate (mother #1087). cursor-mtime measures
    # "time since last turn-end" not "time since last activity"; mid-long-
    # turn cursor is stale even though session is maximally alive. tmux's
    # `#{pane_activity}` covers the mid-turn-active case. If the pane has
    # had activity within `pane_activity_threshold_sec`, suppress A1 — the
    # session is working, not stalled. Falls through to firing A1 when
    # pane_activity is None (tmux missing / older tmux without the format)
    # so F3 is a strengthening of the gate, not a hard dependency.
    pane_age = _pane_activity_age_sec(tmux_session)
    diag["pane_activity_age_sec"] = pane_age
    if pane_age is not None and pane_age < pane_activity_threshold:
        diag["decision"] = "noop_pane_activity_recent"
        _log_event(log_path, "noop", diag, verbose)
        return 0

    diag["decision"] = "a1_send_keys"
    wake_text = (
        f"watchdog wake — cursor stale {cursor_age}s, "
        f"unread={unread}; please drain inbox"
    )
    sent = _tmux_send_keys(tmux_session, wake_text)
    diag["send_keys_ok"] = sent
    if sent:
        _record_a1_fired(marker, cursor_mtime)
    _log_event(log_path, "a1_send_keys", diag, verbose)
    return 1 if sent else 4


_SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
_SYSTEMD_DEFAULT_DIR = Path("/etc/default")
_SYSTEMD_UNIT_NAMES = ("swarph-watchdog.service", "swarph-watchdog.timer")
_SYSTEMD_DEFAULT_NAME = "swarph-watchdog"  # /etc/default/swarph-watchdog
# v0.7.3 bundled template's ExecStart placeholder — substituted at install time
# by `_resolve_swarph_bin()` to the actual binary path on the install host.
# Fixes the v0.7.3 hardcode that broke pipx-installed peers (binary at
# ~/.local/bin/swarph not /usr/local/bin/swarph).
_SWARPH_BIN_PLACEHOLDER = "/usr/local/bin/swarph"


def _resolve_swarph_bin() -> str:
    """Resolve the absolute path of the running swarph binary.

    Resolution order:
      1. ``sys.argv[0]`` if it's an absolute path — most reliable, equals
         the path the user invoked
      2. ``shutil.which(sys.argv[0])`` — bare-name invocation, look up in PATH
      3. ``shutil.which("swarph")`` — generic PATH lookup as fallback
      4. ``/usr/local/bin/swarph`` — last-resort default (matches v0.7.3
         hardcode behavior; no regression if all three above fail)

    ALWAYS returns an absolute path — systemd ExecStart requires absolute.
    Relative inputs (e.g. ``venv/bin/swarph`` from editable installs) get
    abspath'd against cwd. Never raises.
    """
    invoked = sys.argv[0] or "swarph"
    if Path(invoked).is_absolute():
        return invoked
    resolved = shutil.which(invoked) or shutil.which("swarph")
    if not resolved:
        return _SWARPH_BIN_PLACEHOLDER
    return os.path.abspath(resolved)


def _bundled_systemd_files() -> dict[str, str]:
    """Return {filename: content} for the 3 bundled systemd templates.

    Reads from the package's bundled `systemd/` data directory via
    importlib.resources. Works regardless of install method (pipx, pip,
    editable, wheel-from-PyPI).
    """
    try:
        from importlib.resources import files as _files
    except ImportError:  # pragma: no cover — Python <3.9 not supported anyway
        from importlib_resources import files as _files  # type: ignore[no-redef]

    pkg_root = _files("swarph_cli") / "systemd"
    out: dict[str, str] = {}
    for name in (*_SYSTEMD_UNIT_NAMES, "swarph-watchdog.default"):
        out[name] = (pkg_root / name).read_text(encoding="utf-8")
    return out


def run_install_service(args: argparse.Namespace) -> int:
    """Install systemd timer + service for periodic watchdog --check.

    Idempotent: overwrites existing unit files (newer-version semantics).
    Requires sudo for /etc/systemd/system writes unless --dry-run.

    Exit codes:
      0  success (or dry-run completed)
      4  configuration error (non-root without --dry-run)
      5  install error (file write failed / systemctl failed)
    """
    files = _bundled_systemd_files()

    # v0.7.4: substitute the bundled service template's ExecStart placeholder
    # with the actual swarph binary path on this host. Fixes the v0.7.3 hardcode
    # that broke pipx-installed peers (binary at ~/.local/bin/swarph not
    # /usr/local/bin/swarph). Pipx is the recommended install path on droplet
    # + lab, so the hardcode bit BOTH peers on first install attempt today.
    swarph_bin = _resolve_swarph_bin()
    service_content = files[_SYSTEMD_UNIT_NAMES[0]].replace(
        f"ExecStart={_SWARPH_BIN_PLACEHOLDER}",
        f"ExecStart={swarph_bin}",
        1,
    )

    # Template the default file with the requested role
    default_content = files["swarph-watchdog.default"].replace(
        "SWARPH_CELL=lab",
        f"SWARPH_CELL={args.cell}",
        1,
    )

    targets = [
        (_SYSTEMD_UNIT_DIR / _SYSTEMD_UNIT_NAMES[0], service_content),
        (_SYSTEMD_UNIT_DIR / _SYSTEMD_UNIT_NAMES[1], files[_SYSTEMD_UNIT_NAMES[1]]),
        (_SYSTEMD_DEFAULT_DIR / _SYSTEMD_DEFAULT_NAME, default_content),
    ]

    if args.dry_run:
        print(f"# DRY RUN — cell={args.cell} swarph_bin={swarph_bin}", file=sys.stderr)
        for path, content in targets:
            print(f"\n# would write {path}:", file=sys.stderr)
            print(content, file=sys.stderr)
        print(
            "\n# would then run:\n"
            "#   sudo systemctl daemon-reload\n"
            "#   sudo systemctl enable --now swarph-watchdog.timer",
            file=sys.stderr,
        )
        return 0

    if os.geteuid() != 0:
        print(
            "ERROR: --install-service requires root. Re-run with sudo, or use "
            "--dry-run to preview the install without writing.",
            file=sys.stderr,
        )
        return 4

    try:
        for path, content in targets:
            path.write_text(content, encoding="utf-8")
            print(f"wrote {path}", file=sys.stderr)
    except (OSError, PermissionError) as exc:
        print(f"ERROR: failed to write unit files: {exc}", file=sys.stderr)
        return 5

    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "enable", "--now", "swarph-watchdog.timer"],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"ERROR: systemctl failed: {exc}", file=sys.stderr)
        return 5

    print(
        f"\nswarph-watchdog.timer installed + enabled for cell={args.cell}.\n"
        f"  status:  systemctl status swarph-watchdog.timer\n"
        f"  logs:    journalctl -u swarph-watchdog.service -f\n"
        f"           OR /var/log/swarph-watchdog.log\n"
        f"  next:    systemctl list-timers swarph-watchdog.timer",
        file=sys.stderr,
    )
    return 0


def run_watchdog(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[2:]  # skip "swarph watchdog"

    if not argv:
        print(_USAGE, file=sys.stderr)
        return 0

    p = argparse.ArgumentParser(prog="swarph watchdog", add_help=True)
    p.add_argument(
        "--check", action="store_true",
        help="One-shot check (cron-callable; exits with status code).",
    )
    p.add_argument(
        "--install-service", action="store_true",
        help="Install systemd timer + service for periodic --check invocation. "
             "Requires sudo. Closes ev_6954f748 substrate-component-install gap.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="With --install-service: show what would be written without "
             "writing. Useful for review or non-root preview.",
    )
    p.add_argument("--cell", default=os.environ.get("SWARPH_CELL", "lab"))
    p.add_argument("--cursor", default=None)
    p.add_argument("--threshold", type=int, default=_DEFAULT_THRESHOLD_SEC)
    p.add_argument(
        "--pane-activity-threshold",
        type=int,
        default=_DEFAULT_PANE_ACTIVITY_THRESHOLD_SEC,
        help="F3 gate: suppress A1 if tmux pane had activity within this "
             "many seconds (covers mid-long-turn working sessions where "
             "cursor-mtime is stale but session is alive).",
    )
    p.add_argument("--gateway", default=_DEFAULT_GATEWAY_URL)
    p.add_argument("--tmux-session", default=None)
    p.add_argument("--peer", default=None)
    p.add_argument("--no-respawn", action="store_true")
    p.add_argument(
        "--peer-health-poll", action="store_true",
        help="Phase 4 (v0.7.6): also query mesh-gateway /peer-health-events. "
             "On recent usage_limit_reset event, treat sessions as wake-"
             "candidates even before the 30min cursor-staleness threshold. "
             "Requires MESH_GATEWAY_TOKEN in env. Default OFF (opt-in).",
    )
    p.add_argument(
        "--peer-health-window-sec",
        type=int,
        default=_DEFAULT_PEER_HEALTH_WINDOW_SEC,
        help="Phase 4: window for recovery-event lookup; default 600 (10 min).",
    )
    p.add_argument(
        "--peer-health-recovery-threshold",
        type=int,
        default=_DEFAULT_PEER_HEALTH_RECOVERY_THRESHOLD_SEC,
        help="Phase 4: min cursor staleness for recovery event to promote "
             "session to wake-candidate; default 120 (2 min).",
    )
    p.add_argument("--log", default=None)
    p.add_argument("--verbose", action="store_true")

    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if args.install_service:
        return run_install_service(args)

    if not args.check:
        print(_USAGE, file=sys.stderr)
        return 4

    return run_check(args)
