"""Private, pull-only monitor entrypoint for the separate proxy uid."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.error import URLError


def _prepare_state(path, config):
    import grp
    from .local_proxy import safe_parent
    from .proxy_routes import ProxyError

    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.is_relative_to(config.token_file.parent):
        raise ProxyError("proxy_unavailable")
    safe_parent(path / "inbox.log", config.cell_uid)
    group = grp.getgrnam(f"cell-{config.cell}").gr_gid
    info = path.lstat()
    if (info.st_uid != os.geteuid() or info.st_gid != group
            or stat.S_IMODE(info.st_mode) != 0o2750):
        raise ProxyError("proxy_unavailable")
    for parent in (path, *path.parents):
        info = parent.lstat()
        execute = (stat.S_IXUSR if info.st_uid == config.cell_uid else
                   stat.S_IXGRP if info.st_gid == group else stat.S_IXOTH)
        if not info.st_mode & execute:
            raise ProxyError("proxy_unavailable")
    for child in path.iterdir():
        info = child.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or info.st_nlink != 1):
            raise ProxyError("proxy_unavailable")
    fd = os.open(path / "inbox.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT
                 | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_gid != group or stat.S_IMODE(info.st_mode) != 0o640
                or info.st_nlink != 1):
            raise ProxyError("proxy_unavailable")
    finally:
        os.close(fd)
    return path


@contextmanager
def _private_transport(proxy):
    """Constrain the existing engine only in this private foreground process."""
    from . import duplicate_session
    from .commands import mesh
    from .proxy_routes import ProxyError

    base = proxy.config.gateway_url

    def request(method, url, _token, payload=None):
        if not url.startswith(base + "/"):
            raise ProxyError("upstream_error")
        parts = urlsplit(url)
        params = parse_qsl(parts.query, keep_blank_values=True)
        if parts.path == "/messages":
            params = [("to_node" if key == "to" else key, value) for key, value in params]
        path = parts.path + ("?" + urlencode(params) if params else "")
        return proxy.call_gateway(method, path, payload)

    def get(url, token, **_kwargs):
        try:
            status, body = request("GET", url, token)
            if not isinstance(body, dict):
                raise ProxyError("upstream_error")
            return status, body
        except ProxyError as exc:
            status = getattr(exc, "status", None)
            return status if isinstance(status, int) and status >= 400 else 502, {
                "detail": "Gateway request failed.",
            }

    def instance_request(method, url, token, payload=None):
        try:
            return request(method, url, token, payload)[1]
        except ProxyError:
            raise URLError("Gateway request failed.") from None

    saved_get, saved_request = mesh._http_get_json, duplicate_session._request
    mesh._http_get_json, duplicate_session._request = get, instance_request
    try:
        yield
    finally:
        mesh._http_get_json, duplicate_session._request = saved_get, saved_request


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    from .local_proxy import Proxy, harden_process, load_config, read_token
    from .commands import mesh, monitor

    previous_mask = os.umask(0o027)
    previous_rowclear = os.environ.pop("SWARPH_ROWCLEAR", None)
    try:
        harden_process()
        config = load_config(args.cell, args.config)
        state_dir = _prepare_state(args.state_dir, config)
        if monitor._daemon_owns_state_dir(state_dir)[0] is not None:
            print("proxy monitor: state is owned by a daemon", file=sys.stderr)
            return 2
        if mesh.pidfile_status(state_dir / mesh._MONITOR_PIDFILE)[0] == "live_ours":
            print("proxy monitor: already running", file=sys.stderr)
            return 2
        if (state_dir / monitor._SUPERVISION_HOLD).exists():
            print("proxy monitor: supervision hold present", file=sys.stderr)
            return 0
        token = read_token(config)
        with _private_transport(Proxy(config, token)):
            state = mesh.MonitorState(
                self_name=config.cell, state_dir=state_dir,
                gateway=config.gateway_url, token=token,
                sinks=[mesh.parse_sink("pull")],
            )
            return monitor._run_pinned(
                state, state.pidfile_path, config.cell, state.sinks, state.poll_s,
                supervisor=f"proxy-monitor:{config.cell}",
            )
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Neither upstream bodies nor exception text belong in token-uid logs.
        print("proxy monitor: startup or engine failure", file=sys.stderr)
        return 2
    finally:
        os.umask(previous_mask)
        if previous_rowclear is not None:
            os.environ["SWARPH_ROWCLEAR"] = previous_rowclear


if __name__ == "__main__":
    raise SystemExit(main())
