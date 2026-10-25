"""Card #1262: name-or-role parent match, close the commander row on
recovery, and drop peers the gateway does not list.

Each case fails on the parent by assertion.
"""
import datetime
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from swarph_cli.scripts.wake_watchdog import _channel_alive

DEFUNCT = (
    "mistral",
    "crawlcast-api",
    "temp-peer",
    "vite-llm-3826243",
    "vite-llm-3826381",
)


def _yaml(config: Path, name: str, role: str) -> None:
    directory = config / "swarph" / "cells"
    directory.mkdir(parents=True, exist_ok=True)
    cwd = Path.cwd().as_posix()
    (directory / f"{role}.yaml").write_text(
        "schema_version: v1\n"
        f"name: {name}\n"
        f"role: {role}\n"
        f"cwd: \"{cwd}\"\n"
        "provider: claude\n",
        encoding="utf-8",
    )


def _proc(root: Path, pid: int, ppid: int, argv: list[str]) -> None:
    child = root / str(pid)
    child.mkdir(parents=True, exist_ok=True)
    (child / "stat").write_text(f"{pid} (child) S {ppid} 0 0\n", encoding="utf-8")
    parent = root / str(ppid)
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def _heartbeat(inbox: Path, pid: int, ts: float) -> None:
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("", encoding="utf-8")
    (inbox.parent / "channel_heartbeat.json").write_text(
        json.dumps({"pid": pid, "ts": ts}), encoding="utf-8")


def test_unpinned_claude_name_matches_yaml_name_or_role(tmp_path, monkeypatch):
    """science-claude's pane is --name science-claude. The yaml role is
    science, and there is no pinned session. That parent is the session.
    --name equal to the role still is. A different name is not."""
    inbox = tmp_path / "science-claude" / "mesh-sidecar" / "inbox.log"
    now = 1_700_000_100.0
    _heartbeat(inbox, 4242, now - 5)
    proc = tmp_path / "proc"
    _proc(proc, 4242, 111, ["claude", "--name", "science-claude"])
    _yaml(tmp_path / "config", "science-claude", "science")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert _channel_alive(
        str(inbox), now=now, cell="science-claude", proc_root=proc) is True
    _proc(proc, 4242, 111, ["claude", "--name", "science"])
    assert _channel_alive(
        str(inbox), now=now, cell="science-claude", proc_root=proc) is True
    _proc(proc, 4242, 111, ["claude", "--name", "other"])
    assert _channel_alive(
        str(inbox), now=now, cell="science-claude", proc_root=proc) is False


def _stub(tmp_path: Path) -> Path:
    stub = tmp_path / "swarph"
    stub.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        from pathlib import Path
        log = Path(os.environ["STUB_LOG"])
        with log.open("a", encoding="utf-8") as fh:
            fh.write(" ".join(sys.argv[1:]) + "\\n")
        closed = Path(os.environ["STUB_CLOSED"])
        if "obligations" in sys.argv and "list" in sys.argv:
            rows = [] if closed.exists() else [{{
                "id": 42,
                "holder": "commander",
                "card_id": 729,
                "status": "open",
                "what": "lab-ovh DM wake dead since 2026-10-01 00:00:00Z",
            }}]
            print(json.dumps({{"obligations": rows}}))
        if "obligations" in sys.argv and "close" in sys.argv:
            closed.write_text("1", encoding="utf-8")
        raise SystemExit(int(os.environ.get("STUB_RC", "0")))
    """), encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_recovered_sender_closes_its_commander_row_once(tmp_path):
    """The sender's wake is alive again. The open dead-wake row on card 729
    is closed once, and the evidence names the recovery time. A second pass
    does not close again."""
    root = tmp_path / "state"
    inbox = root / "lab-ovh" / "mesh-sidecar" / "inbox.log"
    now = 1_700_000_000.0
    _heartbeat(inbox, 4242, time.time())
    _proc(tmp_path / "proc", 4242, 111, ["claude", "--name", "lab"])
    _yaml(tmp_path / "config", "lab-ovh", "lab")
    state_path = tmp_path / "wake.json"
    state_path.write_text(json.dumps({
        "lab-ovh": {"outage": True, "alerted_at": now - 100, "missing_since": now - 200},
    }), encoding="utf-8")
    log = tmp_path / "stub.log"
    log.write_text("", encoding="utf-8")
    closed = tmp_path / "closed"
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "WAKE_WATCHDOG_NOW": str(now),
        "WAKE_WATCHDOG_PROC": str(tmp_path / "proc"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SWARPH_BIN": str(_stub(tmp_path)),
        "STUB_LOG": str(log),
        "STUB_CLOSED": str(closed),
        "STUB_RC": "0",
    }
    env.pop("SWARPH_SELF", None)
    env.pop("MESH_GATEWAY_URL", None)
    cmd = [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
           "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"]
    subprocess.run(cmd, env=env, check=True)
    text = log.read_text(encoding="utf-8")
    when = datetime.datetime.fromtimestamp(
        now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    assert text.count("obligations close") == 1
    assert f"recovered {when}" in text
    assert "42" in text
    log.write_text("", encoding="utf-8")
    subprocess.run(cmd, env=env, check=True)
    assert "obligations close" not in log.read_text(encoding="utf-8")


def _peers():
    body = json.dumps([
        {"name": "lab-ovh", "enabled": 1},
        {"name": "vite-llm-3826243", "enabled": 0},
    ]).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def test_unregistered_and_disabled_peers_drop_out_of_the_state(tmp_path):
    """A peer missing from the gateway list, or present and disabled, is not
    kept. The five defunct cells leave the state file on that pass."""
    url, httpd = _peers()
    root = tmp_path / "state"
    inbox = root / "lab-ovh" / "mesh-sidecar" / "inbox.log"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("", encoding="utf-8")
    state_path = tmp_path / "wake.json"
    state = {name: {"outage": True, "alerted_at": None, "missing_since": 1.0}
             for name in DEFUNCT}
    state["lab-ovh"] = {"outage": False}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("tok-1262\n", encoding="utf-8")
    log = tmp_path / "stub.log"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "MESH_GATEWAY_URL": url,
        "SWARPH_BIN": str(_stub(tmp_path)),
        "STUB_LOG": str(log),
        "STUB_CLOSED": str(tmp_path / "closed"),
        "STUB_RC": "0",
    }
    env.pop("SWARPH_SELF", None)
    try:
        subprocess.run(
            [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
             "--as", "lab-ovh", "--escalate", "drop-on-meta-edge",
             "--token-file", str(token)],
            env=env, check=True)
    finally:
        httpd.shutdown()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    for name in DEFUNCT:
        assert name not in saved
    assert "lab-ovh" in saved
