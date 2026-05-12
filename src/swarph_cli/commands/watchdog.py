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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


_DEFAULT_THRESHOLD_SEC = 1800  # 30 minutes
_DEFAULT_A1_RETRIES = 3
_DEFAULT_A1_BACKOFF_SEC = 60
_DEFAULT_GATEWAY_URL = "http://localhost:8788"

_USAGE = """\
Usage:
  swarph watchdog --check [--cell ROLE] [--cursor PATH] [--threshold SEC]
                          [--gateway URL] [--tmux-session NAME]
                          [--peer NAME] [--no-respawn]
                          [--log PATH] [--verbose]

Detects stranded Claude sessions (API throttle / harness death) and attempts
recovery via tmux send-keys A1 wake-prompt, escalating to swarph spawn
respawn (A2) on persistent darkness.

Designed for cron invocation:
  */5 * * * * swarph watchdog --check --cell lab >> ~/.local/log/swarph-watchdog.log 2>&1

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
  --log PATH           append diagnostic log; default $XDG_STATE_HOME/swarph/watchdog.log
  --verbose            also write diagnostics to stderr

Exit codes:
  0  no action taken (session healthy or no unread DMs queued)
  1  A1 fired (wake-prompt sent)
  2  A2 fired (full respawn triggered)
  3  detection error (cursor unreadable / gateway unreachable)
  4  configuration error (invalid args, no cell.yaml resolved)
"""


def _now() -> int:
    return int(time.time())


def _stat_mtime(path: Path) -> Optional[int]:
    try:
        return int(path.stat().st_mtime)
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _resolve_cursor_path(role: str, explicit: Optional[str]) -> Path:
    """Resolve cursor file path with documented fallback chain."""
    if explicit:
        return Path(explicit).expanduser()
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    primary = Path(tmpdir) / f"{role}-cursor.json"
    if primary.exists():
        return primary
    # lab-orchestrator's documented cursor path per session_start_reminder.txt
    return Path("/tmp/lab-claude-cursor.json")


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


def _a1_marker_path(log_path: Path, role: str) -> Path:
    """Marker file recording the cursor_mtime at which A1 was last fired.

    Co-located with the watchdog log so it inherits the same XDG_STATE_HOME
    discipline. Cleared on cursor-advance OR A2 escalation. Used to suppress
    repeat A1 fires within the same stale window — fix for the spam incident
    where cron fired A1 every 5min for 65min into an active session's tmux
    input buffer (commander #1092 + droplet #1087).
    """
    return log_path.parent / f"a1-fired-{role}.marker"


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
    cursor = _resolve_cursor_path(role, args.cursor)
    log_path = _resolve_log_path(args.log)
    threshold = args.threshold
    tmux_session = args.tmux_session or role
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

    marker = _a1_marker_path(log_path, role)
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
    p.add_argument("--cell", default=os.environ.get("SWARPH_CELL", "lab"))
    p.add_argument("--cursor", default=None)
    p.add_argument("--threshold", type=int, default=_DEFAULT_THRESHOLD_SEC)
    p.add_argument("--gateway", default=_DEFAULT_GATEWAY_URL)
    p.add_argument("--tmux-session", default=None)
    p.add_argument("--peer", default=None)
    p.add_argument("--no-respawn", action="store_true")
    p.add_argument("--log", default=None)
    p.add_argument("--verbose", action="store_true")

    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if not args.check:
        print(_USAGE, file=sys.stderr)
        return 4

    return run_check(args)
