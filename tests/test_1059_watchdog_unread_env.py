"""#1059 — template watchdog instances exit 7 (noop_unread_unknown):
systemd units run without MESH_GATEWAY_TOKEN, so unread reads None and
A1 stays permanently inert (opencode + cursor-lin, measured live).

Fix: --install-service writes the installer's MESH_GATEWAY_TOKEN into
the per-cell EnvironmentFile default (0600), never into the unit. The
check itself is unchanged — it already reads the token from env.

Seams: the dry-run preview (all three files print to stderr) for the
install shape, and run_check with a stubbed urlopen for the replay
(token flows into the Authorization header, unread is numeric, the
stale-cursor + live-session + idle-pane shape yields the A1 path).
"""
from __future__ import annotations

import io
import json
import os
import urllib.request

from swarph_cli.commands import watchdog as wd
from swarph_cli.commands.watchdog import run_watchdog

TOKEN = "tok-1059-test"


def _dry_sections(capsys, monkeypatch, *, token=True):
    if token:
        monkeypatch.setenv("MESH_GATEWAY_TOKEN", TOKEN)
    else:
        monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    rc = run_watchdog(argv=["--install-service", "--cell", "opencode",
                            "--dry-run"])
    assert rc == 0
    err = capsys.readouterr().err
    sections = {}
    current = None
    for line in err.splitlines():
        if line.startswith("# would write "):
            # Windows stringifies the target path with backslashes;
            # normalize so the section match below holds everywhere.
            current = line.replace("\\", "/")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections, err


def test_install_writes_token_to_default_file(monkeypatch, capsys):
    """The per-cell EnvironmentFile default carries the token."""
    sections, _ = _dry_sections(capsys, monkeypatch, token=True)
    default = [k for k in sections if "/etc/default/" in k]
    assert len(default) == 1, sections.keys()
    body = "\n".join(sections[default[0]])
    assert f"MESH_GATEWAY_TOKEN={TOKEN}" in body


def test_install_never_puts_token_in_unit(monkeypatch, capsys):
    """No token in ExecStart or anywhere in the .service content —
    units are world-readable, the default file is not."""
    sections, _ = _dry_sections(capsys, monkeypatch, token=True)
    for key, lines in sections.items():
        if key.endswith(".service:"):
            assert TOKEN not in "\n".join(lines), key


def test_install_without_token_warns_unread_blind(monkeypatch, capsys):
    """No token in env: loud warning (template runs would exit 7 on
    unread), and no token line minted anywhere."""
    sections, err = _dry_sections(capsys, monkeypatch, token=False)
    body = "\n".join(l for lines in sections.values() for l in lines)
    assert "MESH_GATEWAY_TOKEN=" not in body
    assert "unread" in err.lower()


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_replay_token_env_yields_a1(monkeypatch, tmp_path, capsys):
    """Accept replay: token in env (as a sourced EnvironmentFile gives),
    stale cursor, live session, idle pane, unread=3 -> the A1 path with
    a numeric unread_count — the Authorization header carries the token."""
    seen = {}
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", TOKEN)

    def fake_urlopen(req, timeout=5):
        seen["auth"] = req.get_header("Authorization")
        return _FakeResp(json.dumps(
            {"messages": [{}, {}, {}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{}")
    old = 7200
    os.utime(cursor, (old, old))
    import time as _t
    _t.sleep(0.01)
    monkeypatch.setattr(wd, "_tmux_session_exists", lambda name: True)
    monkeypatch.setattr(wd, "_pane_activity_age_sec", lambda name: 99999.0)
    monkeypatch.setattr(wd, "_tmux_send_keys", lambda *a, **k: True)
    log = tmp_path / "watch.log"
    rc = run_watchdog(argv=[
        "--check", "--cell", "opencode", "--no-respawn", "--verbose",
        f"--cursor={cursor}", "--tmux-session", "opencode",
        "--peer", "opencode", "--gateway", "http://gw:1",
        "--liveness-cmd", "true", f"--log={log}",
        "--threshold", "60", "--pane-activity-threshold", "60"])
    assert seen.get("auth") == f"Bearer {TOKEN}", seen
    out = capsys.readouterr()
    assert '"decision": "a1_send_keys"' in out.out + out.err, out
    assert '"unread_count": 3' in out.out + out.err, out
    assert rc == 1
