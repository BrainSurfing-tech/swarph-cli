"""Card20's credential boundary. Only the proxy process reads the peer token."""
from __future__ import annotations

import ctypes
import dataclasses
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from .proxy_routes import ProxyError, route_for

UPSTREAM_TIMEOUT = 15
CLIENT_TIMEOUT = UPSTREAM_TIMEOUT + 5
MAX_LINE = 1_048_576
ERRORS = {
    "verb_not_allowed": "This verb is not available through the proxy.",
    "bad_args": "The request does not match the published argument schema.",
    "peer_not_allowed": "The socket peer is not the configured cell user.",
    "proxy_unavailable": "The local proxy is unavailable.",
    "upstream_unreachable": "The gateway connection failed before a request was sent.",
    "upstream_ambiguous": "The gateway reply was interrupted; re-read state before retrying a write.",
    "upstream_error": "The gateway did not return a complete successful JSON response.",
}


@dataclasses.dataclass(frozen=True)
class Config:
    cell: str
    cell_uid: int
    gateway_url: str
    token_file: Path
    socket_path: Path
    audit_path: Path


def _cell_name(cell):
    if not isinstance(cell, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}[a-z0-9]|[a-z]", cell):
        raise ProxyError("bad_args")
    return cell


def _cell_can_write(info, cell_uid):
    # Group membership matters as much as owner bits at this uid boundary.
    import pwd
    try:
        peer = pwd.getpwuid(cell_uid)
        groups = os.getgrouplist(peer.pw_name, peer.pw_gid)
    except KeyError:
        groups = []
    return bool(info.st_mode & stat.S_IWOTH or
                info.st_uid == cell_uid and info.st_mode & stat.S_IWUSR or
                info.st_gid in groups and info.st_mode & stat.S_IWGRP)


def safe_parent(path: Path, cell_uid: int):
    """Reject symlinks and cell-writable ancestors, including socket swap sites."""
    if not path.is_absolute():
        raise ProxyError("proxy_unavailable")
    for parent in reversed((path.parent, *path.parent.parents)):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ProxyError("proxy_unavailable")
        if _cell_can_write(info, cell_uid):
            # Sticky /tmp cannot replace a proxy-owned child. The immediate
            # socket/store directory must still never be writable by the cell.
            if parent == path.parent or not (info.st_mode & stat.S_ISVTX and info.st_uid == 0):
                raise ProxyError("proxy_unavailable")


def validate_install(paths, cell_uid):
    """Operator preflight: executable, HOME and import trees cannot trust cell writes."""
    for path in map(Path, paths):
        safe_parent(path, cell_uid)
        for item in (path, *(path.rglob("*") if path.is_dir() else ())):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or _cell_can_write(info, cell_uid):
                raise ProxyError("proxy_unavailable")


def _private_read(path: Path, cell_uid: int, *, token=False):
    safe_parent(path, cell_uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in {0, os.geteuid()}
                or info.st_uid == cell_uid or info.st_mode & 0o022
                or token and (info.st_uid != os.geteuid() or info.st_mode & 0o077)):
            raise ProxyError("proxy_unavailable")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            value = stream.read(MAX_LINE + 1)
        if len(value) > MAX_LINE:
            raise ProxyError("proxy_unavailable")
        return value
    finally:
        if fd >= 0:
            os.close(fd)


def load_config(cell: str, path=None) -> Config:
    """Config, not arguments or environment, chooses the upstream and token file."""
    _cell_name(cell)
    store_root = "/private/var/lib" if sys.platform == "darwin" else "/var/lib"
    path = Path(path or f"{store_root}/swarph-proxy/{cell}/config.json")
    # First open is still non-following and never discloses a parse failure.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LINE:
                raise ProxyError("proxy_unavailable")
            data = json.load(stream)
        uid = data["cell_uid"]
        if type(uid) is not int or uid <= 0 or uid == os.geteuid():
            raise ProxyError("proxy_unavailable")
        # Re-read through the uid-aware check, so the first parse cannot authorize
        # a cell-writable file or a replaced path.
        if json.loads(_private_read(path, uid)) != data:
            raise ProxyError("proxy_unavailable")
        store = path.parent.stat()
        if store.st_uid != os.geteuid() or stat.S_IMODE(store.st_mode) != 0o700:
            raise ProxyError("proxy_unavailable")
        if set(data) - {"cell_uid", "gateway_url", "token_file", "socket_path", "audit_path"}:
            raise ProxyError("proxy_unavailable")
        gateway = data["gateway_url"]
        url = urlsplit(gateway)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                or url.password or url.query or url.fragment or url.path not in {"", "/"}):
            raise ProxyError("proxy_unavailable")
        url.port  # Validate the port before a connection is attempted.
        config = Config(cell, uid, gateway.rstrip("/"),
                        Path(data.get("token_file", path.parent / "peer_token")),
                        Path(data.get("socket_path", f"/run/swarph-proxy/{cell}.sock")),
                        Path(data.get("audit_path", f"/var/log/swarph-proxy/{cell}.jsonl")))
        for local_path in (config.token_file, config.socket_path, config.audit_path):
            safe_parent(local_path, uid)
        return config
    except Exception:
        raise ProxyError("proxy_unavailable") from None


def read_token(config: Config) -> str:
    try:
        store = config.token_file.parent.stat()
        if store.st_uid != os.geteuid() or stat.S_IMODE(store.st_mode) != 0o700:
            raise ProxyError("proxy_unavailable")
        token = _private_read(config.token_file, config.cell_uid, token=True).strip()
        if not token or len(token) > 4096 or any(not 33 <= ord(c) <= 126 for c in token):
            raise ProxyError("proxy_unavailable")
        return token
    except Exception:
        raise ProxyError("proxy_unavailable") from None


def harden_process():
    """Do this before reading credentials; no token-bearing process can dump."""
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            raise ProxyError("proxy_unavailable")
    elif sys.platform == "darwin":
        if libc.ptrace(31, 0, None, 0) != 0:  # PT_DENY_ATTACH
            raise ProxyError("proxy_unavailable")
    else:
        raise ProxyError("proxy_unavailable")


def peer_uid(connection: socket.socket) -> int:
    if sys.platform.startswith("linux"):
        return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise ProxyError("peer_not_allowed")
        return uid.value
    raise ProxyError("peer_not_allowed")


def failure(request_id, error, status=None):
    result = {"request_id": request_id, "ok": False, "error": error, "detail": ERRORS[error]}
    if status is not None:
        result["status"] = status
    return result


def _json(raw):
    def invalid(_):
        raise ValueError("invalid JSON constant")
    return json.loads(raw, parse_constant=invalid)


def _contains_secret(value, token):
    if isinstance(value, str):
        return token in value
    if isinstance(value, dict):
        return any(_contains_secret(key, token) or _contains_secret(item, token) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item, token) for item in value)
    return False


class Proxy:
    def __init__(self, config: Config, token: str):
        self.config = config
        self._token = token

    def call_gateway(self, method: str, path: str, body=None, *, deadline=None):
        """One direct connection, no environment proxies or redirect handling."""
        url = urlsplit(self.config.gateway_url)
        deadline = deadline or time.monotonic() + UPSTREAM_TIMEOUT
        connection = None
        sent = False
        status = None
        expired = threading.Event()
        timer = None
        try:
            remaining = max(0.001, deadline - time.monotonic())
            connection = (http.client.HTTPSConnection(url.hostname, url.port, timeout=remaining)
                          if url.scheme == "https" else
                          http.client.HTTPConnection(url.hostname, url.port, timeout=remaining))
            connected, abandoned, connect_failed = threading.Event(), threading.Event(), threading.Event()
            def connect():
                try:
                    connection.connect()
                except Exception:
                    connect_failed.set()
                finally:
                    if abandoned.is_set():
                        connection.close()
                    connected.set()
            # getaddrinfo can ignore socket timeouts. A late connection is closed
            # without ever sending credentials, rather than blocking this cell.
            connector = threading.Thread(target=connect, daemon=True)
            connector.start()
            if not connected.wait(max(0.001, deadline - time.monotonic())):
                abandoned.set()
                if connected.is_set():
                    connection.close()
                raise ProxyError("upstream_unreachable")
            if connect_failed.is_set():
                raise ProxyError("upstream_unreachable")
            upstream_socket = connection.sock
            def expire():
                expired.set()
                try:
                    upstream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(max(0.001, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            sent = True  # A failing send can still have sent a prefix of a write.
            payload = json.dumps(body, ensure_ascii=True).encode() if body is not None else None
            connection.request(method, path, body=payload,
                               headers={"Authorization": f"Bearer {self._token}",
                                        "Content-Type": "application/json", "Accept": "application/json"})
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            response = connection.getresponse()
            status = response.status
            if not 200 <= status < 300:
                raise ProxyError("upstream_error")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_LINE):
                raise ProxyError("upstream_error")
            raw = response.read(MAX_LINE + 1)
            if expired.is_set():
                raise ProxyError("upstream_ambiguous")
            if len(raw) > MAX_LINE or length is not None and len(raw) != int(length):
                raise ProxyError("upstream_error")
            result = _json(raw)
            # No allowed response may become a credential-reflection channel.
            if _contains_secret(result, self._token):
                raise ProxyError("upstream_error")
            return status, result
        except ProxyError as exc:
            if expired.is_set():
                exc = ProxyError("upstream_ambiguous")
            exc.status = status
            raise exc from None
        except (http.client.IncompleteRead, UnicodeError, ValueError):
            exc = ProxyError("upstream_ambiguous" if expired.is_set() else "upstream_error")
            exc.status = status
            raise exc from None
        except Exception:
            raise ProxyError("upstream_ambiguous" if sent else "upstream_unreachable") from None
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()

    def request(self, request):
        request_id = None
        verb = None
        args = {}
        try:
            if not isinstance(request, dict) or set(request) != {"request_id", "verb", "args"}:
                raise ProxyError("bad_args")
            request_id = str(uuid.UUID(request["request_id"]))
            if request_id != request["request_id"] or self._token in request_id:
                request_id = None
                raise ProxyError("bad_args")
            verb, args = request["verb"], request["args"]
            method, path, body = route_for(verb, args, self.config.cell)
            deadline = time.monotonic() + UPSTREAM_TIMEOUT
            if verb in {"board.cards.move", "board.cards.say"}:
                _, card_path, _ = route_for("board.card.get", {"id": args["id"]}, self.config.cell)
                _, card = self.call_gateway("GET", card_path, deadline=deadline)
                if not isinstance(card, dict):
                    raise ProxyError("upstream_error")
                if verb == "board.cards.move":
                    if type(card.get("stage_version")) is not int or card["stage_version"] < 0:
                        raise ProxyError("verb_not_allowed")
                else:
                    try:
                        thread = str(uuid.UUID(card["thread_uuid"]))
                        recipient = body.get("to_node") or card.get("assignee") or self.config.cell
                        if not isinstance(recipient, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", recipient):
                            raise ValueError
                    except (ValueError, KeyError, TypeError, AttributeError):
                        raise ProxyError("upstream_error") from None
                    body = dict(body, thread_id=thread, to_node=recipient)
            status, result = self.call_gateway(method, path, body, deadline=deadline)
            reply = {"request_id": request_id, "ok": True, "status": status, "body": result}
        except ProxyError as exc:
            reply = failure(request_id, exc.error, getattr(exc, "status", None))
        except Exception:
            reply = failure(request_id, "bad_args")
        self.audit(request_id, verb, args, reply)
        return reply

    def audit(self, request_id, verb, args, reply):
        # Never write caller strings, body, gateway detail or exception text.
        known_verb = verb if isinstance(verb, str) and re.fullmatch(r"[a-z.]{1,64}", verb) else None
        if known_verb and self._token in known_verb:
            known_verb = None
        targets = {key: value for key, value in args.items() if key in {"id", "card_id"} and type(value) is int} if isinstance(args, dict) else {}
        entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "request_id": request_id,
                 "verb": known_verb, "targets": targets,
                 "outcome": "ok" if reply["ok"] else reply["error"], "status": reply.get("status")}
        try:
            safe_parent(self.config.audit_path, self.config.cell_uid)
            flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                fd = os.open(self.config.audit_path, flags | os.O_CREAT | os.O_EXCL, 0o640)
            except FileExistsError:
                fd = os.open(self.config.audit_path, flags)
            else:
                os.fchmod(fd, 0o640)  # The service's private umask must not hide the audit from its group.
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o640):
                    raise ProxyError("proxy_unavailable")
                stream.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except Exception:
            # A send may already have applied. Never invent a safe-to-retry result.
            print("proxy audit unavailable", file=sys.stderr)

    def handle(self, connection):
        with connection:
            connection.settimeout(CLIENT_TIMEOUT)
            try:
                if peer_uid(connection) != self.config.cell_uid:
                    reply = failure(None, "peer_not_allowed")
                    self.audit(None, None, {}, reply)
                else:
                    with connection.makefile("rb") as stream:
                        raw = stream.readline(MAX_LINE + 1)
                    if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                        raise ProxyError("bad_args")
                    reply = self.request(_json(raw))
            except ProxyError as exc:
                reply = failure(None, exc.error)
                self.audit(None, None, {}, reply)
            except Exception:
                reply = failure(None, "bad_args")
                self.audit(None, None, {}, reply)
            try:
                connection.sendall(json.dumps(reply, ensure_ascii=True).encode() + b"\n")
            except OSError:
                pass


def serve(config: Config):
    harden_process()
    proxy = Proxy(config, read_token(config))
    listener = None
    owned = False
    try:
        safe_parent(config.socket_path, config.cell_uid)
        if os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1":
            listener = socket.socket(fileno=3)
            if listener.family != socket.AF_UNIX or listener.getsockname() != str(config.socket_path):
                raise ProxyError("proxy_unavailable")
        else:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old_mask = os.umask(0o117)
            try:
                # Never unlink/chmod an existing path: a stale socket needs operator review.
                listener.bind(str(config.socket_path))
                owned = True
            finally:
                os.umask(old_mask)
            listener.listen(16)
        while True:
            connection, _ = listener.accept()
            proxy.handle(connection)
    finally:
        if listener:
            listener.close()
        if owned:
            config.socket_path.unlink(missing_ok=True)


def call(socket_path: str, verb: str, args: dict):
    request_id = str(uuid.uuid4())
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(CLIENT_TIMEOUT)
            connection.connect(socket_path)
            connection.sendall(json.dumps({"request_id": request_id, "verb": verb, "args": args}).encode() + b"\n")
            with connection.makefile("rb") as stream:
                raw = stream.readline(MAX_LINE + 1)
            result = _json(raw)
            if len(raw) > MAX_LINE or not raw.endswith(b"\n") or not isinstance(result, dict):
                raise ValueError
            return result
    except Exception:
        return failure(request_id, "proxy_unavailable")
