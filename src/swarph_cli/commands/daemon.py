"""``swarph daemon`` — Phase 5.6 foreground drain loop per PLAN.md §16.

The structural retirement of the orphaned-tail-F class. Replaces the
4-layer ``tail -F | grep | Monitor | systemd | cron poll`` stack with
one foreground process that polls the gateway directly and writes
the cursor transactionally (write-and-rename, no half-flushed state).

Liveness check collapses to::

    ps aux | grep '[s]warph daemon'

— zero output = monitoring is down.

Default mode is **surface-only** (DMs printed + logged, never auto-replied).
``--auto-act`` flips on the AI-to-AI default per CLAUDE.md DM SEMANTICS,
routing incoming DMs to handlers registered via ``@swarph.on_dm(...)``.
v0.5.0 ships the daemon + cursor + signals + backoff; handler registration
+ ``MeshClient.watch()`` event stream + REPL drain coroutine + capability
advert + heartbeat self-reporting land in v0.5.1+ per PLAN §16.4 / §16.4a /
§16.4b.

Loud-on-down discipline (PLAN §16.5): the daemon never silently exits.
SIGINT / SIGTERM trigger a clean drain + cursor flush + non-zero shell
liveness signal; uncaught exceptions land on stderr loudly. ``ps aux``
is the only thing that needs to be checked.

Open question §16.7 #2 resolution: ``--auto-act`` default OFF (lab read +
drop's standing-auth lane discretion). Daemon-launchers in §15.4 step 6
include ``--auto-act`` explicitly so AI peers opt in at provisioning time.

Open question §16.7 #3 resolution: cursor format stays single-row JSON
with write-and-rename atomic semantics. If flush fails mid-write the
rename never happens and the previous cursor stands — no append-only
log needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Optional

from swarph_cli import session_bridge, stall_alert
from swarph_cli.delivery_queue import DeliveryQueue


_DEFAULT_POLL_S = 30
_BACKOFF_EMPTY_THRESHOLD = 5  # consecutive empty polls before backing off
_BACKOFF_EMPTY_SECONDS = 60
_BACKOFF_5XX_THRESHOLD_SECONDS = 300  # 5 min of consecutive 5xx
_BACKOFF_5XX_SECONDS = 300
_LOUD_DISCONNECT_SECONDS = 600  # emit loud line every minute past this


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph daemon",
        description=(
            "Phase 5.6 mesh inbox drain daemon per PLAN.md §16. "
            "Foreground process; loud-on-down; transactional cursor."
        ),
    )
    p.add_argument(
        "--state-dir",
        default=None,
        help="state directory containing cursor.json + inbox.log "
        "(default: ~/swarph_state/<self>/).",
    )
    p.add_argument(
        "--self",
        dest="self_name",
        default=None,
        help="canonical name of this peer (default: $SWARPH_SELF or "
        "the directory name of --state-dir).",
    )
    p.add_argument(
        "--gateway",
        default=os.environ.get("MESH_GATEWAY_URL", "http://localhost:8788"),
        help="mesh-gateway base URL.",
    )
    p.add_argument(
        "--token-file",
        default=None,
        help="optional secrets file path (mode 0600 expected).",
    )
    p.add_argument(
        "--poll-seconds",
        type=int,
        default=_DEFAULT_POLL_S,
        help=f"base poll cadence in seconds (default: {_DEFAULT_POLL_S}).",
    )
    p.add_argument(
        "--auto-act",
        action="store_true",
        help="route DMs to registered @swarph.on_dm handlers (v0.5.1+ — "
        "in v0.5.0 this is a documentation flag; surface-only mode runs "
        "regardless).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="run a single poll iteration then exit (test mode).",
    )
    return p


def _resolve_self_name(arg: Optional[str], state_dir: Path) -> str:
    if arg:
        return arg
    env = os.environ.get("SWARPH_SELF")
    if env:
        return env
    return state_dir.name


def _resolve_state_dir(arg: Optional[str], self_name_arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser()
    self_name = self_name_arg or os.environ.get("SWARPH_SELF")
    if self_name:
        return Path.home() / "swarph_state" / self_name
    # Last resort — a self_name is needed to disambiguate; surface error.
    raise SystemExit(
        "swarph daemon: cannot resolve state directory. "
        "Pass --state-dir <path> or set $SWARPH_SELF."
    )


def _resolve_token(token_file_arg: Optional[str]) -> str:
    """Mirror onboard's resolution. env → secrets.toml mode 0600 → prompt."""
    from swarph_cli.commands.onboard import _resolve_token as _onboard_resolve

    return _onboard_resolve(token_file_arg)


# ---------------------------------------------------------------------------
# Cursor — single-row JSON with write-and-rename atomic semantics
# ---------------------------------------------------------------------------


def _read_cursor(path: Path) -> dict:
    if not path.exists():
        return {"last_msg_id": 0, "tasks_snapshot": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Loud — corrupted cursor needs operator attention, not silent reset.
        print(
            f"[swarph-daemon] CORRUPTED cursor at {path}: {exc}. "
            f"Refusing to overwrite. Inspect manually.",
            file=sys.stderr,
            flush=True,
        )
        raise


def _write_cursor_atomic(path: Path, cursor: dict) -> None:
    """Write-and-rename: write to a tmp file in the same dir, then atomic
    rename over the target. Failed mid-write leaves the previous cursor
    intact — open question §16.7 #3 resolution."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(cursor, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX + Windows ≥3.3


# ---------------------------------------------------------------------------
# HTTP — stdlib only, no httpx
# ---------------------------------------------------------------------------


def _http_get(url: str, *, token: str, timeout: float = 10.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            body = {"detail": str(exc)}
        return exc.code, body
    except urllib.error.URLError as exc:
        # Network-level failure — gateway down, DNS, etc.
        return 0, {"detail": str(exc)}


# ---------------------------------------------------------------------------
# Daemon state + loop
# ---------------------------------------------------------------------------


class DaemonState:
    """Mutable state held by the drain loop. Surfaced as a class so tests
    can inspect post-run."""

    def __init__(self, *, self_name: str, state_dir: Path, gateway: str,
                 token: str, poll_s: int, auto_act: bool):
        self.self_name = self_name
        self.state_dir = state_dir
        self.gateway = gateway
        self.token = token
        self.poll_s = poll_s
        self.auto_act = auto_act
        self.cursor_path = state_dir / "cursor.json"
        self.inbox_log_path = state_dir / "inbox.log"
        self.queue = DeliveryQueue(state_dir / "delivery_queue.json")
        # The tmux/psmux session hosting the agent is usually named after the
        # cell, but a cell's mesh id can differ from its session name (verified
        # on lab-ovh: mesh self_name="lab-ovh" but the session is named "lab").
        # SWARPH_SESSION_NAME overrides the session used for pane resolution;
        # defaults to self_name.
        self.session_name = os.environ.get("SWARPH_SESSION_NAME", self_name)
        self.cursor = _read_cursor(self.cursor_path)
        self.consecutive_empty = 0
        self.disconnect_since: Optional[float] = None
        self.iterations = 0
        self.dms_seen = 0
        self.shutdown_requested = False


def _log_dm(state: DaemonState, dm: dict) -> None:
    """Both stdout (visible to operator + journald) AND inbox.log (cursor
    audit trail). Inbox.log is append-only structured JSONL."""
    line = (
        f"[{dm.get('created_at', '?')}] "
        f"id={dm['id']} from={dm.get('from_node')} kind={dm.get('kind')} "
        f"→ {dm['content'][:120]!r}"
    )
    print(line, flush=True)
    state.inbox_log_path.parent.mkdir(parents=True, exist_ok=True)
    with state.inbox_log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dm) + "\n")


def _route_to_handler(state: DaemonState, dm: dict) -> None:
    """Under --auto-act, enqueue the DM for delivery into the live session
    (attempt_delivery runs each tick). Surface-only (no auto-act) is unchanged
    — the DM is already logged by _log_dm; nothing further here."""
    if state.auto_act:
        state.queue.enqueue(dm)


def _render_delivery_block(entries: list) -> str:
    """The compact block injected into the session — the resident model reads
    it as 'you have N mesh DM(s), act per DM SEMANTICS'."""
    lines = [f"📨 mesh delivery ({len(entries)} new):"]
    for e in entries:
        lines.append(
            f"  · from={e.get('from')} kind={e.get('kind')}: {e.get('content','')}"
        )
    lines.append("(act per DM SEMANTICS — reply AI-to-AI via mesh-gateway; "
                 "loop human only across a privilege boundary)")
    return " ".join(lines)


def attempt_delivery(state: DaemonState) -> None:
    """Try to deliver queued DMs into the cell's live session pane. Runs every
    tick. NEVER raises (the daemon must keep draining). Opt-in: no-op unless
    --auto-act. Fail-safe toward defer: only inject on POSITIVE idle."""
    if not state.auto_act:
        return
    try:
        if not state.queue.pending():
            return
        # Wake-gate: only inject when an ACTIONABLE DM is queued. If only
        # ride-along entries (fyi/status/broadcast-answer) are pending, hold
        # them — they deliver in the batch when the next actionable DM wakes
        # the cell. This is what prevents fyi-churn on an idle cell. Not a
        # stall (no deferred bump) — an intentional wait.
        if not state.queue.any_wake():
            return
        pane = session_bridge.resolve_session_pane(state.session_name)
        if pane is None:
            # Headless / non-standard cell — stay surface-only; DMs remain
            # queued (already logged). Not an error.
            return
        st = session_bridge.probe_pane(pane)
        if st == "modal":
            if session_bridge.try_dismiss_safe_modal(pane) and \
                    session_bridge.probe_pane(pane) == "idle":
                st = "idle"
        if st != "idle":
            n = state.queue.bump_deferred()
            if stall_alert.is_alert_tick(n):
                stall_alert.send_stall_alert(
                    state.gateway, state.token, state.self_name,
                    n, len(state.queue.pending()),
                )
            return
        entries = state.queue.pending()
        block = _render_delivery_block(entries)
        if session_bridge.inject(pane, block):
            state.queue.remove({e["id"] for e in entries})
            state.queue.reset_deferred()
        # inject failure → leave queued, retry next tick (no counter bump —
        # the cell was idle; a send failure is transient, not a stall).
    except Exception as exc:  # noqa: BLE001 — bridge must never crash the loop
        print(f"[swarph-daemon] delivery error (continuing): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _select_next_poll_seconds(state: DaemonState) -> int:
    """Backoff per §16.2: empty-poll backoff after 5 consecutive empties,
    5xx backoff after 5 min of consecutive failures."""
    if state.disconnect_since is not None:
        if (time.time() - state.disconnect_since) > _BACKOFF_5XX_THRESHOLD_SECONDS:
            return _BACKOFF_5XX_SECONDS
    if state.consecutive_empty >= _BACKOFF_EMPTY_THRESHOLD:
        return _BACKOFF_EMPTY_SECONDS
    return state.poll_s


async def _drain_iteration(state: DaemonState) -> None:
    """One poll → handle → cursor-write cycle. Errors logged loud; never
    raises out of the loop."""
    state.iterations += 1
    last_id = state.cursor.get("last_msg_id", 0)
    # Note: gateway query param is `to=` (NOT `to_node=`). The latter is
    # silently ignored — bit the entire session's drain code which had
    # python-side filters masking the issue. Defense-in-depth: also
    # filter from_node != self_name client-side in case any future
    # gateway quirk re-introduces outbound bleed-through.
    url = (
        f"{state.gateway}/messages?to={state.self_name}"
        f"&limit=50"
    )
    status, body = _http_get(url, token=state.token)

    if status == 0:
        # Network-level failure
        if state.disconnect_since is None:
            state.disconnect_since = time.time()
        elapsed = time.time() - state.disconnect_since
        if elapsed > _LOUD_DISCONNECT_SECONDS:
            print(
                f"[swarph-daemon] LOUD: gateway unreachable for "
                f"{elapsed:.0f}s — {body.get('detail', '?')}",
                file=sys.stderr,
                flush=True,
            )
        return
    if status >= 500:
        if state.disconnect_since is None:
            state.disconnect_since = time.time()
        print(
            f"[swarph-daemon] gateway 5xx {status}: {body.get('detail', '?')}",
            file=sys.stderr,
            flush=True,
        )
        return
    if status >= 400:
        print(
            f"[swarph-daemon] gateway {status}: {body.get('detail', '?')}",
            file=sys.stderr,
            flush=True,
        )
        return

    # Success — clear disconnect tracking
    state.disconnect_since = None

    messages = [
        m
        for m in body.get("messages", [])
        if m["id"] > last_id and m.get("from_node") != state.self_name
    ]
    if not messages:
        state.consecutive_empty += 1
        return

    # Process oldest-first so cursor monotonically advances
    messages.sort(key=lambda m: m["id"])
    state.consecutive_empty = 0
    new_last_id = last_id
    for dm in messages:
        _log_dm(state, dm)
        _route_to_handler(state, dm)
        state.dms_seen += 1
        new_last_id = max(new_last_id, dm["id"])

    state.cursor["last_msg_id"] = new_last_id
    _write_cursor_atomic(state.cursor_path, state.cursor)


async def _drain_loop(state: DaemonState) -> None:
    """Main loop. Returns on shutdown_requested. Exceptions in
    _drain_iteration are caught + logged + retried; only signal handlers
    set shutdown_requested."""
    print(
        f"[swarph-daemon] starting: self={state.self_name} "
        f"gateway={state.gateway} poll={state.poll_s}s "
        f"state={state.state_dir} auto_act={state.auto_act} "
        f"cursor.last_msg_id={state.cursor.get('last_msg_id', 0)}",
        flush=True,
    )

    while not state.shutdown_requested:
        try:
            await _drain_iteration(state)
        except Exception as exc:  # noqa: BLE001 — loud-on-error per §16.4
            print(
                f"[swarph-daemon] iteration error (continuing): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        attempt_delivery(state)   # deliver queued DMs every tick (guarded, never raises)

        delay = _select_next_poll_seconds(state)
        # Sleep in 1-second chunks so SIGINT/SIGTERM can interrupt promptly
        for _ in range(delay):
            if state.shutdown_requested:
                break
            await asyncio.sleep(1)

    print(
        f"[swarph-daemon] shutdown: iterations={state.iterations} "
        f"dms_seen={state.dms_seen} cursor.last_msg_id={state.cursor.get('last_msg_id', 0)}",
        flush=True,
    )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, state: DaemonState) -> None:
    """SIGINT + SIGTERM → set shutdown_requested. The loop drains cleanly
    on the next sleep boundary (≤1s)."""

    def _handler(signum, frame):  # noqa: ARG001
        if not state.shutdown_requested:
            print(
                f"[swarph-daemon] signal {signum} received — draining + flushing cursor",
                flush=True,
            )
        state.shutdown_requested = True

    # Use the signal module directly rather than loop.add_signal_handler so
    # this works inside test harnesses where the loop's default policy may
    # block signal-handler installation.
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def run_daemon(argv: list[str]) -> int:
    """Entry point invoked by ``swarph_cli.main`` verb dispatch."""
    args = _build_parser().parse_args(argv)

    # Resolve identity + state path
    self_name = args.self_name or os.environ.get("SWARPH_SELF")
    if args.state_dir:
        state_dir = Path(args.state_dir).expanduser()
        if not self_name:
            self_name = state_dir.name
    elif self_name:
        state_dir = Path.home() / "swarph_state" / self_name
    else:
        print(
            "swarph daemon: cannot resolve identity. Pass --self <name> or "
            "--state-dir <path> or set $SWARPH_SELF.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    state_dir.mkdir(parents=True, exist_ok=True)
    token = _resolve_token(args.token_file)
    if not token:
        print("swarph daemon: empty MESH_GATEWAY_TOKEN", file=sys.stderr)
        return 2

    state = DaemonState(
        self_name=self_name,
        state_dir=state_dir,
        gateway=args.gateway,
        token=token,
        poll_s=args.poll_seconds,
        auto_act=args.auto_act,
    )

    if args.once:
        # Test mode — single iteration, no signal handlers, no loop
        asyncio.run(_drain_iteration(state))
        return 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop, state)
    try:
        loop.run_until_complete(_drain_loop(state))
    finally:
        # Final cursor flush in case shutdown happened mid-iteration
        with suppress(Exception):
            _write_cursor_atomic(state.cursor_path, state.cursor)
        loop.close()
    return 0
