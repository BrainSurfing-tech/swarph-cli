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


def test_install_writes_token_file_not_token_value(monkeypatch, capsys):
    """With a token in env, the preview names the 0600 token file but
    never shows the value — and no file section carries a token line."""
    sections, err = _dry_sections(capsys, monkeypatch, token=True)
    assert TOKEN not in err, "token value leaked into preview"
    body = "\n".join(l for lines in sections.values() for l in lines)
    assert "MESH_GATEWAY_TOKEN=" not in body
    assert "0600" in err


def test_install_never_puts_token_in_unit(monkeypatch, capsys):
    """No token in ExecStart or anywhere in the .service content —
    units are world-readable."""
    sections, _ = _dry_sections(capsys, monkeypatch, token=True)
    for key, lines in sections.items():
        if key.endswith(".service:"):
            assert TOKEN not in "\n".join(lines), key


def test_token_file_round_trip_0600_and_newline_refused(monkeypatch, tmp_path):
    """The helper writes the value with mode 600 and refuses multiline
    values instead of writing half a credential."""
    import stat
    import sys
    monkeypatch.setenv("SWARPH_WATCHDOG_TOKEN_FILE",
                       str(tmp_path / "sub" / "watchdog-service.token"))
    path = wd._watchdog_service_token_path()
    wd._write_service_token_file(path, TOKEN)
    assert path.read_text(encoding="utf-8") == TOKEN + "\n"
    if sys.platform != "win32":  # POSIX file-mode bits not representable on Windows
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert wd._read_service_token() == TOKEN
    try:
        wd._write_service_token_file(path, "a\nb")
    except ValueError:
        pass
    else:
        raise AssertionError("multiline token accepted")
    assert path.read_text(encoding="utf-8") == TOKEN + "\n"


def test_install_without_token_warns_unread_blind(monkeypatch, capsys):
    """No token in env: loud warning (template runs would exit 7 on
    unread), and no token line minted anywhere."""
    sections, err = _dry_sections(capsys, monkeypatch, token=False)
    body = "\n".join(l for lines in sections.values() for l in lines)
    assert "MESH_GATEWAY_TOKEN=" not in body
    assert "unread" in err.lower()


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _rig_a1(monkeypatch, tmp_path, stub):
    """Shared A1 replay rig: token in env, stale cursor, live session,
    idle pane. Returns (run, seen) where run() executes the check."""
    seen = {}
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setattr(urllib.request, "urlopen", stub(seen))
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{}")
    os.utime(cursor, (7200, 7200))
    monkeypatch.setattr(wd, "_tmux_session_exists", lambda name: True)
    monkeypatch.setattr(wd, "_pane_activity_age_sec", lambda name: 99999.0)
    monkeypatch.setattr(wd, "_tmux_send_keys", lambda *a, **k: True)
    log = tmp_path / "watch.log"

    def run(capsys):
        rc = run_watchdog(argv=[
            "--check", "--cell", "opencode", "--no-respawn", "--verbose",
            f"--cursor={cursor}", "--tmux-session", "opencode",
            "--peer", "opencode", "--gateway", "http://gw:1",
            "--liveness-cmd", "true", f"--log={log}",
            "--threshold", "60", "--pane-activity-threshold", "60"])
        return rc, capsys.readouterr()
    return run, seen


def test_replay_token_env_yields_a1(monkeypatch, tmp_path, capsys):
    """Accept replay: token in env, stale cursor, live session, idle
    pane, count endpoint says 3 -> the A1 path with a numeric
    unread_count — the Authorization header carries the token, and no
    list query is ever made (no bodies cross the wire)."""
    calls = []

    def stub(seen):
        def fake_urlopen(req, timeout=5):
            seen["auth"] = req.get_header("Authorization")
            calls.append(req.full_url)
            return _FakeResp(json.dumps(
                {"to_node": "opencode", "n": 3}).encode())
        return fake_urlopen

    run, seen = _rig_a1(monkeypatch, tmp_path, stub)
    rc, out = run(capsys)
    assert seen.get("auth") == f"Bearer {TOKEN}", seen
    assert all("/messages/unread-count?" in u for u in calls), calls
    assert '"decision": "a1_send_keys"' in out.out + out.err, out
    assert '"unread_count": 3' in out.out + out.err, out
    assert rc == 1


def test_replay_count_404_falls_back_to_list(monkeypatch, tmp_path, capsys):
    """Mixed fleet: gateway predates the count endpoint (404) -> the
    client falls back to the list query rather than going blind."""
    def stub(seen):
        def fake_urlopen(req, timeout=5):
            if req.full_url.startswith("http://gw:1/messages/unread-count?"):
                raise urllib.error.HTTPError(
                    req.full_url, 404, "nope", {}, None)
            return _FakeResp(json.dumps(
                {"messages": [{}, {}, {}]}).encode())
        return fake_urlopen

    run, _ = _rig_a1(monkeypatch, tmp_path, stub)
    rc, out = run(capsys)
    assert '"decision": "a1_send_keys"' in out.out + out.err, out
    assert '"unread_count": 3' in out.out + out.err, out
    assert rc == 1
