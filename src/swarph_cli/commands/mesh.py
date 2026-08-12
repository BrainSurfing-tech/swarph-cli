"""``swarph mesh`` — provider-agnostic mesh DM tools and the monitor engine.

This module also hosts the DELIVERY ENGINE behind ``swarph monitor`` (card #122).
It lives here, next to the gateway primitives it drives (``_http_get_json``,
``_tmux_wake``, ``_log_dm``), for two reasons:

  1. ``swarph mesh sidecar`` is the deprecated alias for ``swarph monitor start
     --deliver tmux:<target>``, and there must be exactly ONE implementation
     underneath. A second copy is how the deployed :8788 gateway and the CLI's
     forked gateway drifted ~1800 lines apart; we are not repeating that.
  2. The sidecar's regression suites monkeypatch ``mesh._tmux_wake`` /
     ``mesh._http_get_json`` / ``mesh._log_dm``. Those patches only bite if the
     engine's call sites resolve through THIS module's namespace.

``src/swarph_cli/commands/monitor.py`` is the CLI surface (start/status/stop) and
imports from here; the import is one-directional on purpose.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import MutableMapping
from pathlib import Path
from typing import Optional

from .. import tokens
from ._display import sanitize_terminal


def _format_inbox_line(dm: dict) -> str:
    """One inbox line, terminal-safe: peer-authored content/from_node/kind are
    sanitized so a hostile DM can't inject terminal escapes on display (0.29.2)."""
    read = "read" if dm.get("read_at") else "unread"
    content = sanitize_terminal((dm.get("content") or "").replace("\n", " "))[:160]
    who = sanitize_terminal(dm.get("from_node"))
    kind = sanitize_terminal(dm.get("kind"))
    return f"id={dm.get('id')} {read} from={who} kind={kind} {content}"


_DEFAULT_GATEWAY = "http://localhost:8788"
_DEFAULT_POLL_S = 30
_BACKOFF_EMPTY_THRESHOLD = 5
_BACKOFF_EMPTY_SECONDS = 60
_BACKOFF_5XX_THRESHOLD_SECONDS = 300
_BACKOFF_5XX_SECONDS = 300
_DEFAULT_WAKE_MIN_INTERVAL_S = 60


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph mesh",
        description="Provider-agnostic mesh DM commands.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send", help="send a mesh DM")
    send.add_argument("to", help="recipient peer name")
    send.add_argument("--kind", required=True, help="message kind")
    send.add_argument("--content", required=True, help="message body")
    _add_common(send)

    inbox = sub.add_parser("inbox", help="read this peer's mesh inbox")
    inbox.add_argument("--unread", action="store_true", help="only unread DMs")
    inbox.add_argument("--limit", type=int, default=20, help="max messages")
    inbox.add_argument("--json", action="store_true", help="print raw JSON")
    inbox.add_argument(
        "--peek",
        action="store_true",
        help="show the inbox WITHOUT marking anything read (default: reading consumes)",
    )
    _add_common(inbox)

    register = sub.add_parser("register", help="self-register this peer")
    register.add_argument(
        "--url",
        default=None,
        help="peer service URL (default: http://<self>:8787)",
    )
    register.add_argument(
        "--capability",
        action="append",
        default=[],
        help="capability as KEY=VALUE; VALUE parsed as JSON when possible",
    )
    register.add_argument(
        "--force",
        action="store_true",
        help="allow register when a local per-peer token file already exists",
    )
    _add_common(register)

    sidecar = sub.add_parser("sidecar", help="poll inbox and wake a tmux cell")
    sidecar.add_argument("--tmux-target", default=None, help="tmux target pane")
    sidecar.add_argument("--state-dir", default=None, help="state directory")
    sidecar.add_argument("--poll-seconds", type=int, default=_DEFAULT_POLL_S)
    sidecar.add_argument(
        "--wake-min-interval",
        type=int,
        default=_DEFAULT_WAKE_MIN_INTERVAL_S,
        help="minimum seconds between tmux wake prompts",
    )
    sidecar.add_argument("--once", action="store_true", help="poll once and exit")
    _add_common(sidecar)

    return p


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as", dest="self_name", default=None, help="sender/self peer")
    p.add_argument(
        "--gateway",
        default=os.environ.get("MESH_GATEWAY_URL", _DEFAULT_GATEWAY),
        help="mesh-gateway base URL",
    )
    p.add_argument("--token-file", default=None, help="explicit bearer token file")


def _peer_token_path(self_name: str) -> Path:
    return Path.home() / ".config" / "swarph" / f"{self_name}.peer_token"


# _TOKEN_KEYS moved to swarph_cli.tokens.TOKEN_KEYS (#332). Not re-exported:
# a second copy of a constant in a second module is precisely how the two token
# PARSERS diverged in the first place. One definition, one address.


def _read_token_file(path: Path) -> str:
    """Read a bearer token from a token file OR an env-style file.

    THE IMPLEMENTATION MOVED to `swarph_cli.tokens.read_token_file` (#332).
    droplet's "one parser behind one flag" work (2026-07-26) lived here, in a
    COMMAND module \u2014 so every other verb either reimplemented it or reached
    into a sibling command's private helper. gpt-ops flagged that during the
    #332 review: `onboard` must not depend on `mesh`'s internals. The parser is
    byte-for-byte unchanged; only its address is.

    This wrapper stays because it is a PATCH POINT: existing suites monkeypatch
    `mesh._read_token_file`, and deleting the name would make those patches
    silently miss (the same trap documented on `_tmux_wake` above).
    """
    from swarph_cli.tokens import read_token_file

    return read_token_file(path)


def _read_secrets_token(path: Path) -> str:
    if not path.exists():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "MESH_GATEWAY_TOKEN":
            return value.strip().strip('"').strip("'")
    return ""


def _resolve_self_name(
    arg: Optional[str],
    *,
    state_dir: Optional[Path] = None,
) -> str:
    if arg:
        return arg
    env = os.environ.get("SWARPH_SELF")
    if env:
        return env
    if state_dir is not None:
        return state_dir.name
    raise RuntimeError("cannot resolve self identity; pass --as or set SWARPH_SELF")


def _resolve_token(
    self_name: str,
    token_file_arg: Optional[str],
    *,
    allow_peer_token: bool = True,
    identity_is_explicit: bool = True,
) -> str:
    """Delegates to swarph_cli.tokens.resolve_token — see that module for why
    three separate resolvers was the defect, not three separate bugs.

    `identity_is_explicit` defaults True here because every mesh verb reaches
    this through _resolve_self_name, which either took --as, took SWARPH_SELF,
    or derived the name from the state dir the operator pointed at. In all three
    the caller has said which cell it is, so that cell's own credential outranks
    an ambient MESH_GATEWAY_TOKEN — which is the whole fix.

    Parsing goes through tokens.read_token_file — the same strict reader #332
    extracted, which validates latin-1 encodability and names the offending file
    and line. mesh._read_token_file remains as a delegating shim because the
    sidecar suites monkeypatch that name.
    """
    res = tokens.resolve_token(
        self_name,
        token_file_arg,
        env_keys=("MESH_GATEWAY_TOKEN",),
        identity_is_explicit=identity_is_explicit,
        allow_peer_token=allow_peer_token,
        secrets_path=Path.home() / ".swarph" / "secrets.toml",
    )
    if res is not None:
        return res.token
    raise RuntimeError(
        "cannot resolve mesh token; set MESH_GATEWAY_TOKEN or create "
        f"{_peer_token_path(self_name)}"
    )


def _post_json(
    url: str,
    body: dict,
    token: str,
    *,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"detail": str(exc)}
        return exc.code, err_body
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc)}


def _http_get_json(
    url: str,
    token: str,
    *,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"detail": str(exc)}
        return exc.code, err_body
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc)}


def _parse_capability(spec: str) -> tuple[str, object]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"capability {spec!r} not KEY=VALUE shape")
    key, value = spec.split("=", 1)
    try:
        return key.strip(), json.loads(value)
    except json.JSONDecodeError:
        return key.strip(), value


def _write_secret_file_mode_600(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(value)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _run_send(args: argparse.Namespace) -> int:
    self_name = _resolve_self_name(args.self_name)
    token = _resolve_token(self_name, args.token_file)
    body = {
        "from_node": self_name,
        "to_node": args.to,
        "kind": args.kind,
        "content": args.content,
    }
    status, payload = _post_json(
        f"{args.gateway.rstrip('/')}/messages",
        body,
        token,
    )
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>")
        print(f"swarph mesh send: gateway {status}: {detail}", file=sys.stderr)
        return 1
    print(
        f"sent id={payload.get('id')} from={payload.get('from_node')} "
        f"to={payload.get('to_node')} kind={payload.get('kind')}"
    )
    return 0


def _mark_read(gateway: str, token: str, messages: list) -> None:
    """Mark every unread DM we just surfaced as read. Best-effort.

    Reading your inbox consumes it — like any mail client. Without this, a peer's
    request stays "unread" no matter how many times it is answered, and an agent
    whose loop asks "is it still unread?" can never exit. That is not theoretical:
    on 2026-07-10 grok-researcher answered one request 67 times (~410k tokens)
    because the CLI never called the gateway's POST /messages/{id}/read.

    A mark-read failure must NEVER fail the listing — the caller has already seen
    the messages, and losing the read receipt is strictly less bad than losing them.
    """
    base = gateway.rstrip("/")
    failed = []
    for dm in messages:
        if dm.get("read_at"):
            continue
        msg_id = dm.get("id")
        if msg_id is None:
            continue
        try:
            status, _ = _post_json(f"{base}/messages/{msg_id}/read", {}, token)
            if status < 200 or status >= 300:
                failed.append(msg_id)
        except Exception:  # network, DNS, timeout — never break the listing
            failed.append(msg_id)
    if failed:
        print(
            f"swarph mesh inbox: mark-read failed for id(s) {failed} "
            "(messages shown above; they will be re-listed as unread)",
            file=sys.stderr,
        )


def _run_inbox(args: argparse.Namespace) -> int:
    self_name = _resolve_self_name(args.self_name)
    token = _resolve_token(self_name, args.token_file)
    params = {"to": self_name, "limit": str(args.limit)}
    if args.unread:
        params["unread_only"] = "true"
    url = f"{args.gateway.rstrip('/')}/messages?{urllib.parse.urlencode(params)}"
    status, payload = _http_get_json(url, token)
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>")
        print(f"swarph mesh inbox: gateway {status}: {detail}", file=sys.stderr)
        return 1
    messages = payload.get("messages", [])
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        if not getattr(args, "peek", False):
            _mark_read(args.gateway, token, messages)
        return 0
    if not messages:
        print(f"inbox {self_name}: empty")
        return 0
    for dm in messages:
        print(_format_inbox_line(dm))
    if not getattr(args, "peek", False):
        _mark_read(args.gateway, token, messages)
    return 0


def _run_register(args: argparse.Namespace) -> int:
    self_name = _resolve_self_name(args.self_name)
    token_path = _peer_token_path(self_name)
    if token_path.exists() and not args.force:
        print(
            f"swarph mesh register: token file already exists: {token_path}. "
            "Use --force to re-register without clobbering by accident.",
            file=sys.stderr,
        )
        return 1
    token = _resolve_token(self_name, args.token_file, allow_peer_token=False)
    caps = {}
    for spec in args.capability:
        key, value = _parse_capability(spec)
        caps[key] = value
    body = {
        "name": self_name,
        "url": args.url or f"http://{self_name}:8787",
        "capabilities": caps or {"can_claim_tasks": True},
    }
    status, payload = _post_json(
        f"{args.gateway.rstrip('/')}/peers/register",
        body,
        token,
    )
    if status < 200 or status >= 300:
        detail = payload.get("detail", "<gateway error>")
        print(f"swarph mesh register: gateway {status}: {detail}", file=sys.stderr)
        return 1
    peer_token = payload.get("peer_token")
    token_status = payload.get("token_status")
    if peer_token:
        _write_secret_file_mode_600(token_path, peer_token)
        print(
            f"registered {payload.get('name', self_name)} "
            f"token_status={token_status or 'minted'} token_file={token_path}"
        )
        return 0
    print(
        f"registered {payload.get('name', self_name)} "
        f"token_status={token_status or 'existing'}; no new token returned"
    )
    return 0


def _default_sidecar_state_dir(self_name: str) -> Path:
    # NOT renamed to .../monitor when `swarph monitor` shipped (card #122).
    # `swarph init` pins this exact path as the cell's cursor_path and the
    # watchdog reads it (see commands/init.py); renaming it would have silently
    # split every existing cell's cursor from its reader.
    return Path.home() / "swarph_state" / self_name / "mesh-sidecar"


def _read_cursor(path: Path) -> dict:
    if not path.exists():
        return {"last_msg_id": 0, "last_wake_at": 0.0}
    try:
        cursor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[monitor] ignoring unreadable cursor {path}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return {"last_msg_id": 0, "last_wake_at": 0.0}
    if not isinstance(cursor, dict):
        return {"last_msg_id": 0, "last_wake_at": 0.0}
    cursor.setdefault("last_msg_id", 0)
    cursor.setdefault("last_wake_at", 0.0)
    return cursor


def _write_cursor_atomic(path: Path, cursor: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(cursor, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ─────────────────────────────────────────────────────────────────────────────
# card #122 — pluggable delivery
#
# TWO PIECES OF STATE. They answer different questions and must never share a
# variable:
#
#   OBSERVATION CURSOR (cursor.json, `last_msg_id`)
#       what this monitor has READ from the gateway. One per monitor. Advances
#       on observation, ALWAYS, gated on nothing downstream.
#
#   DELIVERY LEDGER (ledgers.json, `last_delivered_id`)
#       what has been DELIVERED to a GIVEN sink. One per sink. Advances only
#       when that sink is satisfied. May lag arbitrarily far.
#
# `pending_wake` (PR #138) was the degenerate one-sink form of the ledger; this
# is that generalized, not a special case bolted beside it. The bug class being
# retired is "one variable, two questions": `last_msg_id` was named a cursor and
# was ALSO a wake receipt, so a dead tmux pane froze it forever and every poll
# re-selected the same DMs with no backoff and no alarm (droplet, 2026-07-26).
#
# Consequence worth stating plainly: NOVELTY IS A PROPERTY OF A SINK'S LEDGER,
# not of the cursor. Attach a sink tomorrow and it replays from inbox.log — the
# data was never consumed, only that sink's pointer is new.
# ─────────────────────────────────────────────────────────────────────────────

_MONITOR_REPLAY_LIMIT = 50   # matches the gateway's 50-message window
_MONITOR_PIDFILE = "monitor.pid"


class MonitorSinkError(RuntimeError):
    """A --deliver spec that must fail LOUDLY (unknown sink, or a held gate)."""


class Sink:
    """A named delivery target with its own independent ledger.

    Subclasses override `deliver` (push) or `observe` (pull). Nothing in the
    engine branches on sink type: a sink that behaves differently says so
    through these flags, so adding one never means special-casing a leg.
    """

    keeps_ledger = True   # False => no ledger, and therefore no unread tracking
    is_push = False       # True => the engine calls deliver() when the ledger lags

    def __init__(self, name: str):
        self.name = name

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> bool:
        """Push `dms` (may be a bounded/empty replay). True == delivered."""
        raise NotImplementedError

    def observe(self, state: "MonitorState", ledger: dict, window: list) -> bool:
        """Called with every poll's raw window. True == this ledger changed."""
        return False

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} DM{plural} pending for {self.name}"


class PullSink(Sink):
    """The DEFAULT. Consumer-pulled: the monitor pushes nothing at all.

    Its ledger advances on ACK — a message being marked read by the gateway when
    the agent runs `swarph mesh inbox` — and NEVER on observation. That is what
    lets `status` answer "you have DMs" as cursor-minus-ledger without inventing
    any new state.

    Why this is the floor and not a nicety: every PUSH sink's liveness is a
    precondition for hearing anything, and the mesh has gone silently deaf twice
    that way (tmux crash kills the wake Monitor, SessionStart drains but does not
    re-arm, and "no wake" is indistinguishable from "no mail"). A pull check run
    BY the cell lives one layer above tmux and cannot die with it.
    """

    is_push = False

    def __init__(self):
        super().__init__("pull")

    def observe(self, state: "MonitorState", ledger: dict, window: list) -> bool:
        observed = int(state.observed.get("last_msg_id", 0))
        mine = sorted(
            (int(m.get("id", 0)), bool(m.get("read_at")))
            for m in window
            if m.get("from_node") != state.self_name
        )
        new = int(ledger["last_delivered_id"])
        for msg_id, is_read in mine:
            # Stop at the OLDEST unread: an ACK on id 62 says nothing about 61.
            if not is_read:
                break
            new = max(new, msg_id)
        else:
            # Nothing unread anywhere in the window -> the consumer is caught up
            # to the cursor, including DMs the window no longer carries.
            new = max(new, observed)
        if new == int(ledger["last_delivered_id"]):
            return False
        ledger["last_delivered_id"] = new
        ledger["last_delivery_at"] = time.time()
        return True

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} unread DM{plural}"


class NoneSink(Sink):
    """Genuinely nothing: observe, append inbox.log, no ledger, no unread track.

    `none` MEANS none. An earlier draft had it keeping a ledger so `status` could
    still report unread; droplet rejected the NAME rather than the mechanism
    (DM #8532), because a flag whose name and behaviour diverge is the bug class
    we spent two days digging out — `count` that was a decaying weight, `MID` that
    straddled a mode boundary, `last_msg_id` that was also a wake receipt. The
    mechanism became `pull`; the word `none` stayed true.
    """

    keeps_ledger = False
    is_push = False

    def __init__(self):
        super().__init__("none")


class TmuxSink(Sink):
    """Poke a tmux pane — the behaviour `mesh sidecar` has always had."""

    is_push = True

    def __init__(self, target: str):
        super().__init__(f"tmux:{target}")
        self.target = target

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> bool:
        # Module-global lookup on purpose: the sidecar regression suites patch
        # `mesh._tmux_wake`, and a `from`-import here would silently bypass them.
        return _tmux_wake(self.target)

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} DM{plural} not yet delivered to {self.name}"


class TmuxNotifySink(Sink):
    """Show a tmux status-line notice without modifying the pane input buffer."""

    is_push = True

    def __init__(self, target: str):
        super().__init__(f"tmux-notify:{target}")
        self.target = target

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> bool:
        # A notification is deliberately content-free: it must not leak DM text
        # into a shared status line, and unlike TmuxSink it never submits input.
        return _tmux_notify(self.target, max(1, len(dms)))

    def pending_label(self, count: int) -> str:
        plural = "s" if count != 1 else ""
        return f"{count} DM{plural} not yet notified to {self.name}"


class StdoutSink(Sink):
    """Write the DM to stdout. Delivery always succeeds."""

    is_push = True

    def __init__(self):
        super().__init__("stdout")

    def _emit(self, line: str) -> None:
        print(line, flush=True)

    def deliver(self, state: "MonitorState", dms: list, up_to_id: int) -> bool:
        if not dms:
            # The ledger says something is owed but inbox.log cannot produce it
            # (log rotated, or `_log_dm` was bypassed). Say so rather than
            # marking the gap delivered in silence.
            self._emit(
                f"[monitor] stdout: {up_to_id} observed but no body recoverable "
                "from inbox.log for the owed range"
            )
            return True
        for dm in dms:
            self._emit(_format_inbox_line(dm))
        return True


def parse_sink(spec: str) -> Sink:
    """`--deliver SINK` -> Sink. Unknown or held specs RAISE; nothing no-ops."""
    if spec == "pull":
        return PullSink()
    if spec == "none":
        return NoneSink()
    if spec == "stdout":
        return StdoutSink()
    if spec.startswith("tmux-notify:"):
        target = spec[len("tmux-notify:"):]
        if not target:
            raise MonitorSinkError(
                "sink 'tmux-notify:' needs a target, e.g. tmux-notify:lab:0.0"
            )
        return TmuxNotifySink(target)
    if spec.startswith("tmux:"):
        target = spec[len("tmux:"):]
        if not target:
            raise MonitorSinkError("sink 'tmux:' needs a target, e.g. tmux:lab:0.0")
        return TmuxSink(target)
    if spec.startswith("webhook:"):
        # HELD by the commander: outward-facing egress, and a build greenlight
        # does not clear an egress gate. It must EXIT, not silently no-op — a
        # held feature that no-ops rots into a phantom capability, where someone
        # configures it, sees no error, and believes it is delivering.
        raise MonitorSinkError(
            "sink 'webhook:' is HELD pending the commander's egress gate "
            "(card #122); it is not implemented and will not be silently "
            "ignored. Use --deliver pull / tmux:<target> / stdout / none."
        )
    raise MonitorSinkError(
        f"unknown sink {spec!r}; expected pull, none, stdout, tmux:<target>, "
        "or tmux-notify:<target>"
    )


def _new_ledger() -> dict:
    return {
        "last_delivered_id": 0,
        "last_delivery_at": 0.0,
        "consecutive_failures": 0,
        "created_at": time.time(),
    }


def _read_ledgers(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Losing a ledger costs at most a duplicate delivery; refusing to start
        # costs every delivery. Say it loudly and carry on.
        print(f"[monitor] ignoring unreadable ledgers {path}: {exc}",
              file=sys.stderr, flush=True)
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, led in data.items():
        if not isinstance(led, dict):
            continue
        base = _new_ledger()
        base.update(led)
        out[str(name)] = base
    return out


def _write_ledgers_atomic(path: Path, ledgers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(ledgers, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _replay_from_inbox_log(
    path: Path, after_id: int, limit: int
) -> tuple[list, int]:
    """Owed DMs newer than `after_id`, newest `limit` kept. Returns (dms, skipped).

    This is BOTH the restart-retry path and the late-attached-sink replay path.
    They are the same question — "what does this ledger still owe" — so they get
    one mechanism; two would drift.
    """
    if not path.exists():
        return [], 0
    keep: deque = deque(maxlen=max(0, limit))
    total = 0
    try:
        with path.open("r", encoding="utf-8") as fp:
            for raw in fp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    dm = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if int(dm.get("id", 0)) <= after_id:
                    continue
                total += 1
                keep.append(dm)
    except OSError as exc:
        print(f"[monitor] cannot replay {path}: {exc}", file=sys.stderr, flush=True)
        return [], 0
    return list(keep), total - len(keep)


class MonitorState:
    def __init__(
        self,
        *,
        self_name: str,
        state_dir: Path,
        gateway: str,
        token: str,
        sinks: list,
        poll_s: int = _DEFAULT_POLL_S,
        min_interval_s: int = _DEFAULT_WAKE_MIN_INTERVAL_S,
        log_prefix: str = "[monitor]",
        replay_limit: int = _MONITOR_REPLAY_LIMIT,
    ):
        self.self_name = self_name
        self.state_dir = Path(state_dir)
        self.gateway = gateway.rstrip("/")
        self.token = token
        self.sinks = list(sinks)
        self.poll_s = poll_s
        self.min_interval_s = min_interval_s
        self.log_prefix = log_prefix
        self.replay_limit = replay_limit
        self.cursor_path = self.state_dir / "cursor.json"
        self.ledgers_path = self.state_dir / "ledgers.json"
        self.inbox_log_path = self.state_dir / "inbox.log"
        self.pidfile_path = self.state_dir / _MONITOR_PIDFILE
        # `observed` is the real dict; `cursor` is what the engine touches. The
        # deprecated sidecar swaps `cursor` for a view (see _LegacyCursorView).
        self.observed = _read_cursor(self.cursor_path)
        self.cursor = self.observed
        self.ledgers = _read_ledgers(self.ledgers_path)
        if not self.ledgers_path.exists() and "pending_wake" in self.observed:
            self._migrate_pre_122_ledger()
        # Ledgers are keyed by the sink STRING, so renaming a pane creates a
        # fresh ledger that replays from zero. That is a deliberate trade (no ids
        # for an operator to manage) — `status` reports `ledger_missing` so the
        # surprise replay is visible rather than mysterious.
        self.new_ledgers = {s.name for s in self.sinks
                            if s.keeps_ledger and s.name not in self.ledgers}
        self.deliveries: dict = {}
        self.consecutive_empty = 0
        self.disconnect_since: Optional[float] = None
        self.shutdown_requested = False
        self.iterations = 0
        self.dms_seen = 0

    def _migrate_pre_122_ledger(self) -> None:
        """Adopt a pre-card-#122 cursor's `pending_wake` as the initial ledger.

        A `pending_wake` key in cursor.json with no ledgers.json beside it means
        this state dir was last written by the one-sink sidecar, where the ledger
        WAS that boolean. Without this, every upgraded peer's first poll would
        see `last_delivered_id=0` against a cursor in the thousands, fire a
        redundant wake and report a capped replay of mail it already delivered.

        Applied uniformly to every ledger-keeping sink -- this is the degenerate
        one-sink ledger being generalized, not a per-sink special case.
        """
        observed = int(self.observed.get("last_msg_id", 0))
        seed = 0 if self.observed.get("pending_wake") else observed
        for sink in self.sinks:
            if not sink.keeps_ledger:
                continue
            led = _new_ledger()
            led["last_delivered_id"] = seed
            led["last_delivery_at"] = float(self.observed.get("last_wake_at", 0.0))
            self.ledgers[sink.name] = led

    def ledger(self, name: str) -> dict:
        led = self.ledgers.get(name)
        if led is None:
            led = _new_ledger()
            self.ledgers[name] = led
        return led


def _log_dm(state: MonitorState, dm: dict) -> None:
    """Append to inbox.log for EVERY observed DM, whatever the sinks do.

    This archive is the thing that makes a late-attached sink able to replay, and
    the thing that makes an owed delivery survive a restart. It is written before
    any delivery is attempted, on purpose.
    """
    state.inbox_log_path.parent.mkdir(parents=True, exist_ok=True)
    with state.inbox_log_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(dm) + "\n")
    print(
        f"{state.log_prefix} id={dm.get('id')} from={dm.get('from_node')} "
        f"kind={dm.get('kind')}",
        flush=True,
    )


def _tmux_wake(target: str) -> bool:
    """Inject the wake prompt and submit it using Codex's double-Enter gesture."""
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", "check mesh"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        # In Codex's multiline composer the first Enter creates a line boundary;
        # a consecutive Enter submits the prompt.
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[monitor] tmux wake failed: {exc}", file=sys.stderr, flush=True)
        return False


def _tmux_notify(target: str, dm_count: int) -> bool:
    """Show a transient tmux notice without sending any pane keystrokes."""
    plural = "s" if dm_count != 1 else ""
    try:
        subprocess.run(
            ["tmux", "display-message", "-t", target,
             f"mesh: {dm_count} new DM{plural}; inbox pending"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[monitor] tmux notification failed: {exc}", file=sys.stderr, flush=True)
        return False


def _select_next_poll_seconds(state: MonitorState) -> int:
    if state.disconnect_since is not None:
        if time.time() - state.disconnect_since > _BACKOFF_5XX_THRESHOLD_SECONDS:
            return _BACKOFF_5XX_SECONDS
    if state.consecutive_empty >= _BACKOFF_EMPTY_THRESHOLD:
        return _BACKOFF_EMPTY_SECONDS
    return state.poll_s


def _monitor_deliver(state: MonitorState) -> None:
    """Best-effort delivery to every PUSH sink whose ledger lags the cursor.

    NEVER touches the observation cursor. A failed delivery (dead pane) and a
    deferred one (idle guard) both leave the ledger where it was, so the next
    iteration retries — re-selection is not the retry mechanism, the ledger is.
    That distinction is load-bearing: the gateway only returns the last 50
    messages, so a long-throttled delivery whose message rolled out of the window
    would otherwise be lost outright.
    """
    observed = int(state.observed.get("last_msg_id", 0))
    now = time.time()
    changed = False

    for sink in state.sinks:
        if not sink.keeps_ledger:
            continue
        led = state.ledger(sink.name)
        if sink.is_push:
            delivered = int(led["last_delivered_id"])
            if delivered >= observed:
                continue          # this sink owes nothing
            if now - float(led["last_delivery_at"]) < state.min_interval_s:
                print(f"{state.log_prefix} wake suppressed by idle guard for "
                      f"{sink.name} (cursor advanced; delivery still pending)",
                      flush=True)
                continue
            dms, skipped = _replay_from_inbox_log(
                state.inbox_log_path, delivered, state.replay_limit
            )
            if skipped:
                # A silent cap reads as "delivered everything" when it did not.
                print(f"{state.log_prefix} REPLAY CAPPED for {sink.name}: "
                      f"skipped {skipped} older DM(s) above id {delivered}; "
                      f"delivering the newest {len(dms)} (limit "
                      f"{state.replay_limit}) -- see {state.inbox_log_path}",
                      file=sys.stderr, flush=True)
            if sink.deliver(state, dms, observed):
                led["last_delivered_id"] = observed
                led["last_delivery_at"] = now
                led["consecutive_failures"] = 0
                state.deliveries[sink.name] = state.deliveries.get(sink.name, 0) + 1
            else:
                # A dead sink is VISIBLE instead of silently freezing anything.
                led["consecutive_failures"] = int(led["consecutive_failures"]) + 1
                print(f"{state.log_prefix} DELIVERY FAILED to {sink.name} "
                      f"({led['consecutive_failures']} consecutive) -- DMs up to "
                      f"id {observed} were still observed and archived; the sink "
                      f"is probably gone (session restart / renamed target)",
                      file=sys.stderr, flush=True)
            changed = True

    if changed:
        _write_ledgers_atomic(state.ledgers_path, state.ledgers)


def _monitor_iteration(state: MonitorState) -> None:
    state.iterations += 1
    last_id = int(state.observed.get("last_msg_id", 0))
    # NO unread_only: novelty is the `id > last_msg_id` cursor below, not the read
    # flag. Since `mesh inbox` now marks read (INCIDENT 2026-07-10), a DM read before
    # the next poll would vanish from an unread-filtered query and the cell would
    # never be woken. The two mechanisms must stay orthogonal.
    params = {"to": state.self_name, "limit": "50"}
    url = f"{state.gateway}/messages?{urllib.parse.urlencode(params)}"
    status, body = _http_get_json(url, state.token)
    if status == 0:
        if state.disconnect_since is None:
            state.disconnect_since = time.time()
        return
    if status >= 500:
        if state.disconnect_since is None:
            state.disconnect_since = time.time()
        print(f"{state.log_prefix} gateway {status}: {body.get('detail', '?')}",
              file=sys.stderr)
        return
    if status >= 400:
        print(f"{state.log_prefix} gateway {status}: {body.get('detail', '?')}",
              file=sys.stderr)
        return

    state.disconnect_since = None
    window = body.get("messages", [])
    messages = [
        m
        for m in window
        if int(m.get("id", 0)) > last_id and m.get("from_node") != state.self_name
    ]

    if messages:
        messages.sort(key=lambda m: int(m["id"]))
        state.consecutive_empty = 0
        new_last_id = last_id
        for dm in messages:
            _log_dm(state, dm)
            state.dms_seen += 1
            new_last_id = max(new_last_id, int(dm["id"]))

        # ── BOOKKEEPING: advance the cursor because the DMs were OBSERVED ──
        # Unconditional, and BEFORE any sink is touched. This previously happened
        # inside `if _tmux_wake(...)`, so a gone pane froze it forever; that is
        # the same shape as the 2026-07-10 grok-researcher incident (read-marking
        # tied to listing; one request answered 67 times, ~410k tokens).
        # Delivery is delivery; the cursor is bookkeeping.
        state.observed["last_msg_id"] = new_last_id
        _write_cursor_atomic(state.cursor_path, dict(state.cursor))
    else:
        state.consecutive_empty += 1

    # Pull-shaped sinks read the raw window (ACKs live in `read_at`).
    acked = False
    for sink in state.sinks:
        if sink.keeps_ledger and sink.observe(state, state.ledger(sink.name), window):
            acked = True
    if acked:
        _write_ledgers_atomic(state.ledgers_path, state.ledgers)

    # A delivery deferred by the idle guard (or failed against a dead sink) must
    # still land even though no NEW mail arrived, so this runs on every poll.
    _monitor_deliver(state)

    # MATERIALIZE new ledgers even if nothing moved them. Found by driving the
    # real CLI: a pure `--deliver pull` monitor never writes ledgers.json until
    # somebody ACKs, so `status` reported "no ledger on disk" -- the
    # late-attached-sink warning -- forever, on the DEFAULT path. A warning that
    # fires when nothing is wrong is a warning nobody reads when something is.
    if state.ledgers and not state.ledgers_path.exists():
        _write_ledgers_atomic(state.ledgers_path, state.ledgers)
        state.new_ledgers = set()


def _monitor_loop(state: MonitorState) -> int:
    print(
        f"{state.log_prefix} starting self={state.self_name} "
        f"sinks={','.join(s.name for s in state.sinks)} "
        f"gateway={state.gateway} state={state.state_dir}",
        flush=True,
    )
    while not state.shutdown_requested:
        try:
            _monitor_iteration(state)
        except KeyboardInterrupt:
            state.shutdown_requested = True
            break
        except Exception as exc:
            print(f"{state.log_prefix} iteration error: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        time.sleep(_select_next_poll_seconds(state))
    return 0


# ── pidfile: `start` must be safe to call unconditionally from a hook ────────

def _proc_cmdline(pid: int) -> Optional[str]:
    """The process's argv, space-joined. None when it cannot be determined."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fp:
            raw = fp.read()
    except OSError:
        return None            # not Linux, or the process is gone
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip() or None


# Liveness is TRI-STATE. "I could not determine" is not "dead" — see _process_liveness.
LIVENESS_ALIVE = "alive"
LIVENESS_DEAD = "dead"
LIVENESS_UNKNOWN = "unknown"


def _windows_liveness(pid: int) -> str:
    """Ask Windows directly, via OpenProcess — never via os.kill.

    PROCESS_QUERY_LIMITED_INFORMATION (0x1000) is the minimum right that answers
    "does this pid exist", and it is grantable across sessions where the rights
    os.kill needs are not. ERROR_ACCESS_DENIED means the process EXISTS and we may
    not inspect it — that is ALIVE, not dead. ERROR_INVALID_PARAMETER means no such
    pid. Anything else is UNKNOWN, never dead.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                                  # noqa: BLE001
        return LIVENESS_UNKNOWN
    ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, STILL_ACTIVE = 5, 87, 259
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                return LIVENESS_ALIVE                  # exists; we just cannot look
            if err == ERROR_INVALID_PARAMETER:
                return LIVENESS_DEAD                   # no such pid
            return LIVENESS_UNKNOWN
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return LIVENESS_UNKNOWN
            return LIVENESS_ALIVE if code.value == STILL_ACTIVE else LIVENESS_DEAD
        finally:
            k32.CloseHandle(handle)
    except Exception:                                  # noqa: BLE001
        return LIVENESS_UNKNOWN


def _process_liveness(pid: int) -> str:
    """'alive' | 'dead' | 'unknown'.

    >>> CARD #195 — `os.kill(pid, 0)` IS NOT A LIVENESS PROBE ON WINDOWS, AND ITS
    FAILURE WAS BEING SWALLOWED INTO "DEAD". <<< Measured by workstation-lc against
    a monitor that was demonstrably draining:

        pid 6120 alive per Windows ................. True
        os.kill(6120, 0) from another process ...... OSError [WinError 87]
                                                     -> `except OSError: return False`
        pid 6120 after the probe ................... STILL ALIVE

    The error appears when probing a process the caller did NOT spawn — the worker
    is a grandchild of a detached cmd.exe, a different session/handle-rights context.
    His first throwaway test passed because he probed his own CHILD: a child-process
    control cannot surface this, which is why it read safe.

    ONE LINE PRODUCED BOTH SYMPTOMS: `monitor status` reported "not running" about
    every healthy monitor, AND the single-instance guard believed nothing was
    running, so it never blocked a second start. False negative and duplicate
    spawning from the same swallow.

    >>> THE RULE: "COULD NOT DETERMINE" MUST NEVER BE REPORTED AS "DEAD" BY A GUARD
    WHOSE JOB IS TO PREVENT A SECOND START. <<< A blocked start is visible and
    recoverable; a duplicate is silent and produces two writers over one cursor.
    So callers treat UNKNOWN as occupied, and `status` says so rather than claiming
    the process is running.
    """
    if sys.platform == "win32":
        return _windows_liveness(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return LIVENESS_DEAD
    except PermissionError:
        return LIVENESS_ALIVE       # exists, owned by another user
    except OSError:
        return LIVENESS_UNKNOWN     # NOT dead — we simply could not tell
    return LIVENESS_ALIVE


def _process_alive(pid: int) -> bool:
    """Back-compat bool. UNKNOWN counts as alive — see _process_liveness for why
    treating an undetermined answer as "dead" is the failure mode, not the safe one."""
    return _process_liveness(pid) in (LIVENESS_ALIVE, LIVENESS_UNKNOWN)


def _terminate(pid: int) -> None:
    """SIGTERM, isolated in its own function so `stop`'s one dangerous call is a
    single named seam -- easy to see in review, easy to pin in a test."""
    import signal

    os.kill(pid, signal.SIGTERM)


def read_pidfile(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return rec if isinstance(rec, dict) and isinstance(rec.get("pid"), int) else None


def pidfile_status(path: Path) -> tuple[str, Optional[dict]]:
    """One of 'absent' | 'live_ours' | 'stale' | 'foreign', plus the record.

    'foreign' exists because adopting a live PID we cannot PROVE is ours is how
    `stop` ends up killing something unrelated. When ownership cannot be decided
    we return 'foreign', which means: reclaim the FILE, never signal the PROCESS.
    """
    rec = read_pidfile(path)
    if rec is None:
        return "absent", None
    pid = int(rec["pid"])
    if not _process_alive(pid):
        return "stale", rec
    recorded = rec.get("cmdline")
    current = _proc_cmdline(pid)
    if not recorded:
        # Written on a platform with no /proc. Fall back to the identity we did
        # record; this is weaker than a cmdline match and deliberately last.
        return ("live_ours", rec) if current is None else ("foreign", rec)
    if current is not None and current == recorded:
        return "live_ours", rec
    return "foreign", rec


def write_pidfile(path: Path, *, self_name: str, sinks: list, poll_s: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "pid": os.getpid(),
        "self": self_name,
        "sinks": [s.name if isinstance(s, Sink) else str(s) for s in sinks],
        "poll_s": poll_s,
        "started_at": time.time(),
        "cmdline": _proc_cmdline(os.getpid()),
    }
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ── the deprecated `swarph mesh sidecar` surface ─────────────────────────────

class _LegacyCursorView(MutableMapping):
    """`mesh sidecar`'s flat cursor dict, COMPUTED over the card-#122 split.

    Its callers (and three regression suites) read `cursor["last_msg_id"]`,
    `cursor["pending_wake"]` and `cursor["last_wake_at"]`. Under the split those
    are two different questions living in two files: `last_msg_id` is the
    OBSERVATION cursor, the other two are the tmux sink's LEDGER.

    They are computed here rather than mirrored, because keeping a second copy in
    sync is precisely the "one variable, two questions" trap the card removes.
    """

    _KEYS = ("last_msg_id", "last_wake_at", "pending_wake")

    def __init__(self, state: "MeshSidecarState", observed: dict, sink_name: str):
        self._state = state
        self._observed = observed
        self._sink = sink_name

    def _led(self) -> dict:
        return self._state.ledger(self._sink)

    def _observed_id(self) -> int:
        return int(self._observed.get("last_msg_id", 0))

    def __getitem__(self, key):
        if key == "last_msg_id":
            return self._observed.get("last_msg_id", 0)
        if key == "last_wake_at":
            return float(self._led()["last_delivery_at"])
        if key == "pending_wake":
            return int(self._led()["last_delivered_id"]) < self._observed_id()
        raise KeyError(key)

    def __setitem__(self, key, value):
        if key == "last_msg_id":
            was_pending = self["pending_wake"]
            self._observed["last_msg_id"] = value
            if not was_pending:
                # Legacy semantics: in the flat dict, assigning `last_msg_id`
                # never CREATED an owed wake -- `pending_wake` was a separate
                # field that stayed False. Keep the ledger in step so an outside
                # caller seeding the cursor does not conjure a phantom delivery.
                # (The engine writes `state.observed` directly and never lands
                # here, so a genuinely new observation still owes.)
                self._led()["last_delivered_id"] = int(value)
            return
        if key == "last_wake_at":
            self._led()["last_delivery_at"] = float(value)
            return
        if key == "pending_wake":
            self._led()["last_delivered_id"] = 0 if value else self._observed_id()
            return
        raise KeyError(key)

    def __delitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(self._KEYS)

    def __len__(self):
        return len(self._KEYS)


class MeshSidecarState(MonitorState):
    """DEPRECATED compat facade: a monitor with exactly one `tmux:` sink.

    Kept because droplet and gpu-wsl are running `swarph mesh sidecar` right now;
    breaking their command to rename a verb is not a trade worth making.
    """

    def __init__(
        self,
        *,
        self_name: str,
        state_dir: Path,
        gateway: str,
        token: str,
        tmux_target: str,
        poll_s: int,
        wake_min_interval_s: int,
    ):
        sink = TmuxSink(tmux_target)
        super().__init__(
            self_name=self_name,
            state_dir=Path(state_dir),
            gateway=gateway,
            token=token,
            sinks=[sink],
            poll_s=poll_s,
            min_interval_s=wake_min_interval_s,
            log_prefix="[mesh-sidecar]",
        )
        self.tmux_target = tmux_target
        self.wake_min_interval_s = wake_min_interval_s
        self._sink_name = sink.name
        self.cursor = _LegacyCursorView(self, self.observed, sink.name)

    @property
    def wakes_sent(self) -> int:
        return self.deliveries.get(self._sink_name, 0)

    @property
    def consecutive_wake_failures(self) -> int:
        return int(self.ledger(self._sink_name)["consecutive_failures"])


# Legacy aliases. One engine underneath, so the deprecated verb cannot drift.
_sidecar_iteration = _monitor_iteration
_sidecar_deliver_wake = _monitor_deliver


def _run_sidecar(args: argparse.Namespace) -> int:
    """DEPRECATED alias for `swarph monitor start --deliver tmux:<target>` (#122)."""
    state_dir_arg = Path(args.state_dir).expanduser() if args.state_dir else None
    self_name = _resolve_self_name(args.self_name, state_dir=state_dir_arg)
    state_dir = state_dir_arg or _default_sidecar_state_dir(self_name)
    tmux_target = args.tmux_target or os.environ.get("SWARPH_TMUX_TARGET")
    if not tmux_target:
        print(
            "swarph mesh sidecar: pass --tmux-target or set SWARPH_TMUX_TARGET",
            file=sys.stderr,
        )
        return 2
    # STDERR, never stdout -- stdout is a sink now.
    print(
        "swarph mesh sidecar: DEPRECATED (card #122). Use:  swarph monitor start "
        f"--deliver tmux:{tmux_target}   (or --deliver pull for a silent "
        "'you have DMs' check that survives a dead pane)",
        file=sys.stderr,
    )
    token = _resolve_token(self_name, args.token_file)
    state = MeshSidecarState(
        self_name=self_name,
        state_dir=state_dir,
        gateway=args.gateway,
        token=token,
        tmux_target=tmux_target,
        poll_s=args.poll_seconds,
        wake_min_interval_s=args.wake_min_interval,
    )
    if args.once:
        # Module-global call: `--once` is the seam the sidecar suites patch.
        _sidecar_iteration(state)
        return 0
    write_pidfile(
        state.pidfile_path,
        self_name=self_name,
        sinks=state.sinks,
        poll_s=args.poll_seconds,
    )
    try:
        return _monitor_loop(state)
    finally:
        if pidfile_status(state.pidfile_path)[0] == "live_ours":
            state.pidfile_path.unlink(missing_ok=True)


def run_mesh(argv: list[str]) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "send":
            return _run_send(args)
        if args.command == "inbox":
            return _run_inbox(args)
        if args.command == "register":
            return _run_register(args)
        if args.command == "sidecar":
            return _run_sidecar(args)
        parser.error(f"unknown command: {args.command}")
    except RuntimeError as exc:
        print(f"swarph mesh: {exc}", file=sys.stderr)
        return 2
    return 2
