"""B1: public cell-side CLI, filesystem/uid and tmux transport boundaries."""
import json
import os
from pathlib import Path
import subprocess
import socket
import stat
import time
import urllib.request
from types import SimpleNamespace

import pytest

if os.name != "posix":
    pytest.skip("cell-side wake requires POSIX users and tmux", allow_module_level=True)

import pwd

from swarph_cli.commands import monitor

FIXTURES = Path(__file__).parent / "fixtures"
_RUN = subprocess.run


@pytest.fixture
def cell(monkeypatch, tmp_path):
    inbox = tmp_path / "proxy" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_text(json.dumps({"id": 7, "content": "untrusted fixture"}) + "\n")
    state = tmp_path / "state"
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    ctx = SimpleNamespace(inbox=inbox, state=state, uid=888, directory_uid=777,
                          file_uid=777, calls=[])
    monkeypatch.setattr(os, "geteuid", lambda: ctx.uid)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="cell-opencode"))
    for method in ("stat", "lstat"):
        original = getattr(Path, method)

        def stat(path, *args, _original=original, **kwargs):
            result = _original(path, *args, **kwargs)
            if path in (inbox, inbox.parent):
                fields = list(result)
                fields[4] = ctx.file_uid if path == inbox else ctx.directory_uid
                return os.stat_result(fields)
            return result

        monkeypatch.setattr(Path, method, stat)

    def forbidden(argv, *args, **kwargs):
        ctx.calls.append(argv)
        pytest.fail("refusal must precede every tmux call")

    monkeypatch.setattr(subprocess, "run", forbidden)
    ctx.args = ["wake-from-inbox", "--cell", "opencode", "--inbox", str(inbox),
                "--pane", "opencode:0.0", "--once"]
    return ctx


@pytest.mark.parametrize("kind,code", (("root", 77), ("directory", 78), ("file", 79)))
def test_owner_refusals_have_distinct_codes_before_any_tmux(cell, kind, code):
    if kind == "root":
        cell.uid = 0
    elif kind == "directory":
        cell.directory_uid = cell.uid
        cell.inbox.unlink()  # Directory ownership must still refuse without a file.
    else:
        cell.file_uid = cell.uid
    assert monitor.run_monitor(cell.args) == code
    assert cell.calls == []


@pytest.mark.parametrize("account", ("proxy-opencode", "cell-other", "ubuntu", None))
def test_only_the_exact_cell_account_can_reach_tmux(cell, monkeypatch, account):
    def lookup(uid):
        if account is None:
            raise KeyError(uid)
        return SimpleNamespace(pw_name=account)

    monkeypatch.setattr(pwd, "getpwuid", lookup)
    assert monitor.run_monitor(cell.args) == 80
    assert cell.calls == []


@pytest.mark.parametrize("kind,code", (("root", 77), ("directory", 78), ("file", 79)))
def test_owner_refusals_precede_account_refusal(cell, monkeypatch, kind, code):
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="proxy-opencode"))
    if kind == "root":
        cell.uid = 0
    elif kind == "directory":
        cell.directory_uid = cell.uid
    else:
        cell.file_uid = cell.uid
    assert monitor.run_monitor(cell.args) == code
    assert cell.calls == []


@pytest.fixture
def pane(cell, monkeypatch, tmp_path):
    """Run a real fake tmux executable; never touch a server or live pane."""
    cell.state.mkdir()
    idle = (FIXTURES / "cell-wake-20/idle-real-20261115.txt").read_text()
    (cell.state / "idle.txt").write_text(idle)
    (cell.state / "pane.txt").write_text(idle)
    fake = tmp_path / "tmux-fixture"
    fake.write_text("#!/usr/bin/env python3\n" + r'''
import json, os, sys
from pathlib import Path
p = Path(os.environ['STATE_DIRECTORY'])
a = sys.argv[1:]
cursor = p / 'cursor.json'
with (p / 'calls.jsonl').open('a') as out:
    out.write(json.dumps({'argv': a, 'cursor': cursor.read_text() if cursor.exists() else None}) + '\n')
if (p / 'unavailable').exists():
    print((p / 'unavailable').read_text(), file=sys.stderr)
    sys.exit(1)
if a[0] == 'capture-pane':
    print((p / 'pane.txt').read_text(), end='')
elif a[0] == 'display-message':
    print('1' if (p / 'copy-mode').exists() else '0')
elif a[0] == 'send-keys':
    if '-l' in a:
        lines = (p / 'pane.txt').read_text().splitlines()
        mode = max(i for i, line in enumerate(lines) if line.strip().startswith('┃  Build'))
        lines[mode - 1] = '  ┃  ' + a[-1]
        (p / 'pane.txt').write_text('\n'.join(lines) + '\n')
    elif a[-1] == 'Enter':
        (p / 'pane.txt').write_text((p / 'idle.txt').read_text())
''')
    fake.chmod(0o755)
    # conftest blocks bare tmux calls. Route only that transport to this executable.
    def run(argv, *args, **kwargs):
        assert argv[0] == "tmux", "cell wake must only launch tmux"
        cell.calls.append(argv)
        return _RUN([str(fake), *argv[1:]], *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    return cell


def _events(cell):
    path = cell.state / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_real_idle_capture_wakes_initial_backlog_then_commits(pane):
    pane.inbox.write_text(''.join(json.dumps({"id": i, "content": "NEVER-TYPE-ME"}) + "\n"
                                  for i in (7, 8, 7, 6)))
    assert monitor.run_monitor(pane.args) == 0
    text_sends = [e for e in _events(pane) if "-l" in e["argv"]]
    assert len(text_sends) == 1
    assert text_sends[0]["argv"][-1] == "check mesh: swarph_dm_unread"
    assert text_sends[0]["cursor"] is None
    assert (pane.state / "cursor.json").is_file()
    assert monitor.run_monitor(pane.args) == 0
    assert len([e for e in _events(pane) if "-l" in e["argv"]]) == 1


def _text_sends(cell):
    return [e for e in _events(cell) if "-l" in e["argv"]]


def test_real_running_turn_defers_until_the_real_idle_screen_returns(pane):
    running = (FIXTURES / "cell-wake-20/running-real-20261115.txt").read_text()
    (pane.state / "pane.txt").write_text(running)
    assert monitor.run_monitor(pane.args) == 0
    assert not any(e["argv"][0] == "send-keys" for e in _events(pane))
    assert not (pane.state / "cursor.json").exists()
    assert (pane.state / "heartbeat.json").exists()
    (pane.state / "pane.txt").write_text((pane.state / "idle.txt").read_text())
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 1
    assert json.loads((pane.state / "cursor.json").read_text())["seen_ids"] == [7]


def test_real_permission_busy_defers_without_acknowledging_then_recovers(pane):
    busy = (FIXTURES / "pr514-hunt-794/o_B1_permission_real_verbatim.txt").read_text()
    (pane.state / "pane.txt").write_text(busy)
    assert monitor.run_monitor(pane.args) == 0
    assert _text_sends(pane) == []
    assert not (pane.state / "cursor.json").exists()
    (pane.state / "pane.txt").write_text((pane.state / "idle.txt").read_text())
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 1


def test_generic_clear_pane_is_not_an_opencode_gate(pane):
    (pane.state / "pane.txt").write_text(">\n")
    assert monitor.run_monitor(pane.args) == 0
    assert _text_sends(pane) == []
    assert not (pane.state / "cursor.json").exists()


def test_partial_tail_waits_for_newline_and_out_of_order_ids_are_not_dropped(pane):
    pane.inbox.write_bytes(b'{"id":9}\n{"id":4')
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 1
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 1
    with pane.inbox.open("ab") as out:
        out.write(b'}\n')
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2
    with pane.inbox.open("a") as out:
        out.write('{"id":4}\n{"id":9}\n')
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2


@pytest.mark.parametrize("kind", ("rotation", "truncate", "copy-truncate-regrow"))
def test_reopen_keeps_ids_and_raises_exactly_one_standing_wake(pane, kind):
    with pane.inbox.open("a") as out:
        out.write('{"id":6}\n')
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 1
    if kind == "rotation":
        pane.inbox.rename(pane.inbox.with_suffix(".old"))
        pane.inbox.write_text('{"id":7}\n')
    elif kind == "truncate":
        pane.inbox.write_text("")
    else:
        data = pane.inbox.read_text().replace('"id": 7', '"id": 8')
        assert len(data) == pane.inbox.stat().st_size
        pane.inbox.write_text(data)
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2
    with pane.inbox.open("a") as out:
        out.write('{"id":7}\n{"id":6}\n')
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2


def test_inode_change_with_identical_bytes_still_raises_once(pane):
    data = pane.inbox.read_bytes()
    assert monitor.run_monitor(pane.args) == 0
    pane.inbox.rename(pane.inbox.with_suffix(".old"))
    pane.inbox.write_bytes(data)
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2


def test_crash_after_inject_before_commit_has_one_repeat_not_a_drop(pane, monkeypatch):
    replace = os.replace

    def crash(source, target):
        if Path(target).name == "cursor.json":
            raise RuntimeError("simulated process crash after tmux submission")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        monitor.run_monitor(pane.args)
    assert len(_text_sends(pane)) == 1
    assert not (pane.state / "cursor.json").exists()
    monkeypatch.setattr(os, "replace", replace)
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 2


@pytest.mark.parametrize("reason", ("can't find pane", "can't find session", "no server running"))
def test_missing_tmux_target_is_a_loop_deferral_then_recovers(pane, monkeypatch, reason):
    (pane.state / "unavailable").write_text(reason)
    ticks = []

    def stop_after_two(_seconds):
        ticks.append(True)
        if len(ticks) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", stop_after_two)
    assert monitor.run_monitor(pane.args[:-1]) == 0
    assert len(ticks) == 2
    assert (pane.state / "heartbeat.json").is_file()
    assert not (pane.state / "cursor.json").exists()
    (pane.state / "unavailable").unlink()
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 1


def test_cursor_is_atomically_replaced_after_submission_and_heartbeat_is_local(pane, monkeypatch):
    replace = os.replace
    replacements = []

    def observe(source, target):
        source, target = Path(source), Path(target)
        assert source.parent == target.parent == pane.state
        if target.name == "cursor.json":
            assert _text_sends(pane), "no unseen id may be persisted before submission"
        replacements.append(target.name)
        return replace(source, target)

    monkeypatch.setattr(os, "replace", observe)
    assert monitor.run_monitor(pane.args) == 0
    assert replacements.count("cursor.json") == 1
    assert replacements.count("heartbeat.json") == 1
    heartbeat = json.loads((pane.state / "heartbeat.json").read_text())
    assert heartbeat["at"] > 0


def test_wake_cli_never_resolves_gateway_token_or_generic_pane(pane, monkeypatch):
    from swarph_cli import main, session_bridge, tokens
    from swarph_cli.commands import mesh

    def forbidden(*args, **kwargs):
        pytest.fail("cell-side wake crossed gateway/token/network/generic-pane boundary")

    monkeypatch.setattr(monitor, "env_gateway", forbidden)
    monkeypatch.setattr(tokens, "resolve_token", forbidden)
    monkeypatch.setattr(tokens, "read_token_file", forbidden)
    monkeypatch.setattr(mesh, "_http_get_json", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(session_bridge, "probe_pane", forbidden)
    connect = socket.socket.connect

    def no_network(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            forbidden()
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert main.main(["monitor", *pane.args]) == 0
    assert len(_text_sends(pane)) == 1


def test_only_state_directory_is_opened_for_writing(pane, monkeypatch):
    opened = os.open
    writes = []

    def confined(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            path = Path(path)
            if path != Path(os.devnull):  # subprocess stdio sink, not persistent state
                assert path.parent == pane.state
                writes.append(path)
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", confined)
    assert monitor.run_monitor(pane.args) == 0
    assert writes and (pane.state / "heartbeat.json").exists()


def test_opened_inbox_owned_by_reader_refuses_before_tmux(pane, monkeypatch):
    fstat = os.fstat
    inode = pane.inbox.stat().st_ino

    def owned(fd):
        info = fstat(fd)
        if info.st_ino == inode:
            values = list(info)
            values[4] = pane.uid
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", owned)
    assert monitor.run_monitor(pane.args) == 79
    assert pane.calls == []


def test_owner_is_rechecked_after_read_before_any_capture(pane, monkeypatch):
    fstat = os.fstat
    inode = pane.inbox.stat().st_ino

    def change_owner(fd):
        info = fstat(fd)
        if info.st_ino == inode:
            pane.directory_uid = pane.uid
        return info

    monkeypatch.setattr(os, "fstat", change_owner)
    assert monitor.run_monitor(pane.args) == 78
    assert pane.calls == []


def test_own_cursor_symlink_is_not_a_way_to_read_outside_state(pane):
    assert monitor.run_monitor(pane.args) == 0
    path = pane.state / "cursor.json"
    victim = pane.state.parent / "peer_token"
    victim.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(victim)
    with pane.inbox.open("a") as out:
        out.write('{"id":8}\n')
    count = len(_text_sends(pane))
    assert monitor.run_monitor(pane.args) == 2
    assert len(_text_sends(pane)) == count


def test_only_nonsensitive_heartbeat_is_public_cursor_stays_private(pane):
    assert monitor.run_monitor(pane.args) == 0
    heartbeat = pane.state / "heartbeat.json"
    assert set(json.loads(heartbeat.read_text())) == {"at"}
    assert stat.S_IMODE(heartbeat.stat().st_mode) == 0o644
    assert stat.S_IMODE((pane.state / "cursor.json").stat().st_mode) == 0o600


def test_existing_standing_wake_is_submitted_without_stacking_text(pane):
    path = pane.state / "pane.txt"
    lines = path.read_text().splitlines()
    mode = max(i for i, line in enumerate(lines) if line.strip().startswith("┃  Build"))
    lines[mode - 1] = "  ┃  check mesh: swarph_dm_unread"
    path.write_text("\n".join(lines) + "\n")
    assert monitor.run_monitor(pane.args) == 0
    assert _text_sends(pane) == []
    assert any(e["argv"][-1] == "Enter" for e in _events(pane))
    assert json.loads((pane.state / "cursor.json").read_text())["seen_ids"] == [7]


def test_copy_mode_defers_without_typing_or_committing(pane):
    (pane.state / "copy-mode").touch()
    assert monitor.run_monitor(pane.args) == 0
    assert not any(e["argv"][0] == "send-keys" for e in _events(pane))
    assert not (pane.state / "cursor.json").exists()


def test_strict_helper_rechecks_real_busy_screen_after_outer_idle_gate(pane, monkeypatch):
    run = subprocess.run
    captures = 0

    def change_screen(argv, *args, **kwargs):
        nonlocal captures
        if argv[1] == "capture-pane":
            captures += 1
            if captures == 3:
                (pane.state / "pane.txt").write_text(
                    (FIXTURES / "pr514-hunt-794/o_B1_permission_real_verbatim.txt").read_text())
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", change_screen)
    assert monitor.run_monitor(pane.args) == 0
    assert captures == 3
    assert not any(e["argv"][0] == "send-keys" for e in _events(pane))
    assert not (pane.state / "cursor.json").exists()


def test_timed_out_send_defers_without_committing_and_recovers(pane, monkeypatch):
    run = subprocess.run

    def timeout_send(argv, *args, **kwargs):
        if argv[1] == "send-keys":
            assert kwargs["timeout"] == 5
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timeout_send)
    assert monitor.run_monitor(pane.args) == 0
    assert not (pane.state / "cursor.json").exists()
    monkeypatch.setattr(subprocess, "run", run)
    assert monitor.run_monitor(pane.args) == 0
    assert len(_text_sends(pane)) == 1


def test_missing_inbox_defers_while_heartbeat_advances_then_first_file_is_read(pane, monkeypatch):
    data = pane.inbox.read_bytes()
    pane.inbox.unlink()
    ticks = []

    def create_inbox(seconds):
        if seconds != 1:  # tmux settle sleeps are not poll ticks
            return
        ticks.append(True)
        if len(ticks) == 1:
            assert (pane.state / "heartbeat.json").exists()
            assert pane.calls == []
            pane.inbox.write_bytes(data)
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", create_inbox)
    assert monitor.run_monitor(pane.args[:-1]) == 0
    assert len(_text_sends(pane)) == 1
    assert (pane.state / "cursor.json").exists()


@pytest.mark.parametrize("value", ("", "relative", "/tmp/a:/tmp/b"))
def test_state_directory_is_required_and_absolute_before_writes(cell, monkeypatch, value):
    monkeypatch.setenv("STATE_DIRECTORY", value)
    with pytest.raises(SystemExit) as exc:
        monitor.run_monitor(cell.args)
    assert exc.value.code == 2
    assert not cell.state.exists()
    assert cell.calls == []
