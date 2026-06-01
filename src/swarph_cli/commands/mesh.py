"""``swarph mesh`` — provider-agnostic mesh DM tools and sidecar wake loop."""

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
from pathlib import Path
from typing import Optional


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


def _read_token_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read token file {path}: {exc}") from exc


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
) -> str:
    if token_file_arg:
        return _read_token_file(Path(token_file_arg).expanduser())
    env = os.environ.get("MESH_GATEWAY_TOKEN")
    if env:
        return env
    if allow_peer_token:
        peer_token = _peer_token_path(self_name)
        if peer_token.exists():
            return _read_token_file(peer_token)
    secrets = _read_secrets_token(Path.home() / ".swarph" / "secrets.toml")
    if secrets:
        return secrets
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
        print(f"swarph mesh send: gateway {status}: {payload.get('detail', payload)}", file=sys.stderr)
        return 1
    print(
        f"sent id={payload.get('id')} from={payload.get('from_node')} "
        f"to={payload.get('to_node')} kind={payload.get('kind')}"
    )
    return 0


def _run_inbox(args: argparse.Namespace) -> int:
    self_name = _resolve_self_name(args.self_name)
    token = _resolve_token(self_name, args.token_file)
    params = {"to": self_name, "limit": str(args.limit)}
    if args.unread:
        params["unread_only"] = "true"
    url = f"{args.gateway.rstrip('/')}/messages?{urllib.parse.urlencode(params)}"
    status, payload = _http_get_json(url, token)
    if status < 200 or status >= 300:
        print(f"swarph mesh inbox: gateway {status}: {payload.get('detail', payload)}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    messages = payload.get("messages", [])
    if not messages:
        print(f"inbox {self_name}: empty")
        return 0
    for dm in messages:
        read = "read" if dm.get("read_at") else "unread"
        content = (dm.get("content") or "").replace("\n", " ")
        print(
            f"id={dm.get('id')} {read} from={dm.get('from_node')} "
            f"kind={dm.get('kind')} {content[:160]}"
        )
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
        print(f"swarph mesh register: gateway {status}: {payload.get('detail', payload)}", file=sys.stderr)
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
    return Path.home() / "swarph_state" / self_name / "mesh-sidecar"


def _read_cursor(path: Path) -> dict:
    if not path.exists():
        return {"last_msg_id": 0, "last_wake_at": 0.0}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_cursor_atomic(path: Path, cursor: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(cursor, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class MeshSidecarState:
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
        self.self_name = self_name
        self.state_dir = state_dir
        self.gateway = gateway.rstrip("/")
        self.token = token
        self.tmux_target = tmux_target
        self.poll_s = poll_s
        self.wake_min_interval_s = wake_min_interval_s
        self.cursor_path = state_dir / "cursor.json"
        self.inbox_log_path = state_dir / "inbox.log"
        self.cursor = _read_cursor(self.cursor_path)
        self.consecutive_empty = 0
        self.disconnect_since: Optional[float] = None
        self.shutdown_requested = False
        self.iterations = 0
        self.dms_seen = 0
        self.wakes_sent = 0


def _log_dm(state: MeshSidecarState, dm: dict) -> None:
    state.inbox_log_path.parent.mkdir(parents=True, exist_ok=True)
    with state.inbox_log_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(dm) + "\n")
    print(
        f"[mesh-sidecar] id={dm.get('id')} from={dm.get('from_node')} "
        f"kind={dm.get('kind')}",
        flush=True,
    )


def _tmux_wake(target: str) -> bool:
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "check mesh", "Enter"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[mesh-sidecar] tmux wake failed: {exc}", file=sys.stderr, flush=True)
        return False


def _select_next_poll_seconds(state: MeshSidecarState) -> int:
    if state.disconnect_since is not None:
        if time.time() - state.disconnect_since > _BACKOFF_5XX_THRESHOLD_SECONDS:
            return _BACKOFF_5XX_SECONDS
    if state.consecutive_empty >= _BACKOFF_EMPTY_THRESHOLD:
        return _BACKOFF_EMPTY_SECONDS
    return state.poll_s


def _sidecar_iteration(state: MeshSidecarState) -> None:
    state.iterations += 1
    last_id = int(state.cursor.get("last_msg_id", 0))
    params = {"to": state.self_name, "unread_only": "true", "limit": "50"}
    url = f"{state.gateway}/messages?{urllib.parse.urlencode(params)}"
    status, body = _http_get_json(url, state.token)
    if status == 0:
        if state.disconnect_since is None:
            state.disconnect_since = time.time()
        return
    if status >= 500:
        if state.disconnect_since is None:
            state.disconnect_since = time.time()
        print(f"[mesh-sidecar] gateway {status}: {body.get('detail', '?')}", file=sys.stderr)
        return
    if status >= 400:
        print(f"[mesh-sidecar] gateway {status}: {body.get('detail', '?')}", file=sys.stderr)
        return

    state.disconnect_since = None
    messages = [
        m
        for m in body.get("messages", [])
        if int(m.get("id", 0)) > last_id and m.get("from_node") != state.self_name
    ]
    if not messages:
        state.consecutive_empty += 1
        return

    messages.sort(key=lambda m: int(m["id"]))
    state.consecutive_empty = 0
    new_last_id = last_id
    for dm in messages:
        _log_dm(state, dm)
        state.dms_seen += 1
        new_last_id = max(new_last_id, int(dm["id"]))

    now = time.time()
    last_wake_at = float(state.cursor.get("last_wake_at", 0.0))
    if now - last_wake_at >= state.wake_min_interval_s:
        if _tmux_wake(state.tmux_target):
            state.cursor["last_wake_at"] = now
            state.wakes_sent += 1
    else:
        print("[mesh-sidecar] wake suppressed by idle guard", flush=True)

    state.cursor["last_msg_id"] = new_last_id
    _write_cursor_atomic(state.cursor_path, state.cursor)


def _run_sidecar(args: argparse.Namespace) -> int:
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
        _sidecar_iteration(state)
        return 0
    print(
        f"[mesh-sidecar] starting self={self_name} target={tmux_target} "
        f"gateway={args.gateway} state={state_dir}",
        flush=True,
    )
    while not state.shutdown_requested:
        try:
            _sidecar_iteration(state)
        except KeyboardInterrupt:
            state.shutdown_requested = True
            break
        except Exception as exc:
            print(f"[mesh-sidecar] iteration error: {type(exc).__name__}: {exc}", file=sys.stderr)
        time.sleep(_select_next_poll_seconds(state))
    return 0


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
