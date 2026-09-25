"""Card #960. Eight guards. Each one is the line a can-fail removes."""
from __future__ import annotations

import json
import os
from pathlib import Path

from swarph_cli.channel import PROTOCOL_VERSION, Channel, capabilities, initialize_result
from swarph_cli.commands.spawn import _build_claude_argv
from swarph_cli.scripts.wake_watchdog import _watching


def _row(i, to, kind="status", body="hello", frm="lab-ovh"):
    return json.dumps({
        "id": i, "from_node": frm, "to_node": to, "kind": kind, "content": body, "card": 1,
    })


def _chan(tmp_path, lines, cell="cursor-lin"):
    inbox = tmp_path / "inbox.log"
    inbox.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return Channel(
        cell, inbox,
        tmp_path / "channel_cursor.json",
        tmp_path / "channel_heartbeat.json",
    )


def test_legacy_era_is_pinned_and_permission_is_absent(monkeypatch):
    monkeypatch.setenv("SWARPH_CHANNEL", "allowlisted")
    result = initialize_result(1)["result"]
    monkeypatch.setenv("SWARPH_CHANNEL", "dev")
    assert "claude/channel" in json.dumps(initialize_result(1)["result"]["capabilities"])
    monkeypatch.setenv("SWARPH_CHANNEL", "allowlisted")
    result = initialize_result(1)["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION == "2025-06-18"
    assert result["protocolVersion"] != "2026-07-28"
    blob = json.dumps(capabilities())
    assert "claude/channel" in blob
    assert "permission" not in blob


def test_first_start_does_not_replay(tmp_path):
    chan = _chan(tmp_path, [_row(1, "cursor-lin"), _row(2, "cursor-lin")])
    notes = chan.poll(now=1_000)
    assert [n["params"]["content"] for n in notes] == ["armed: cursor-lin, cursor 2"]
    assert chan.cursor["last_pushed_id"] == 2


def test_resume_does_not_push_the_same_id(tmp_path):
    chan = _chan(tmp_path, [_row(1, "cursor-lin")])
    chan.poll(now=1_000)
    chan.inbox.write_text(_row(1, "cursor-lin") + "\n" + _row(2, "cursor-lin") + "\n", encoding="utf-8")
    chan.cursor["offset"] = 0
    notes = chan.poll(now=2_000)
    bodies = [n["params"]["content"] for n in notes]
    assert sum("id=1 " in b for b in bodies) == 0
    assert any("id=2 " in b for b in bodies)


def test_foreign_to_node_is_dropped(tmp_path):
    chan = _chan(tmp_path, [])
    chan.poll(now=1_000)
    chan.inbox.write_text(_row(9, "other-cell") + "\n", encoding="utf-8")
    chan.cursor["offset"] = 0
    notes = chan.poll(now=2_000)
    blob = json.dumps(notes)
    assert "other-cell" not in blob or "dropped" in blob
    assert "id=9 " not in blob
    assert chan.foreign == 1


def test_frame_contains_the_data_marker(tmp_path):
    chan = _chan(tmp_path, [])
    chan.poll(now=1_000)
    chan.inbox.write_text(_row(3, "cursor-lin", body="ping") + "\n", encoding="utf-8")
    chan.cursor["offset"] = 0
    notes = chan.poll(now=2_000)
    dm = [n for n in notes if "id=3 " in n["params"]["content"]]
    assert dm and "DATA from lab-ovh" in dm[0]["params"]["content"]
    assert dm[0]["params"]["meta"]["from_node"] == "lab-ovh"
    assert all(isinstance(value, str) for value in dm[0]["params"]["meta"].values())


def test_receipts_are_filtered(tmp_path):
    chan = _chan(tmp_path, [])
    chan.poll(now=1_000)
    line = json.dumps({
        "id": 4, "from_node": "lab-ovh", "to_node": "cursor-lin",
        "kind": "status", "content": "receipt: delivered",
    })
    chan.inbox.write_text(line + "\n", encoding="utf-8")
    chan.cursor["offset"] = 0
    notes = chan.poll(now=2_000)
    assert all("id=4 " not in n["params"]["content"] for n in notes)


def test_heartbeat_is_written(tmp_path):
    chan = _chan(tmp_path, [])
    chan.poll(now=5_000)
    data = json.loads(chan.heartbeat_path.read_text(encoding="utf-8"))
    assert data["ts"] == 5_000
    assert "pid" in data


def test_a_fresh_channel_heartbeat_counts_as_a_watcher(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox.log"
    inbox.write_text("", encoding="utf-8")
    hb = tmp_path / "channel_heartbeat.json"
    hb.write_text(json.dumps({"ts": 10_000, "pid": 1}), encoding="utf-8")
    monkeypatch.setattr("swarph_cli.scripts.wake_watchdog.time.time", lambda: 10_100)
    cmd = f"python3 -m swarph_cli.channel {inbox}"
    assert _watching(str(inbox), [cmd]) is True
    monkeypatch.setattr("swarph_cli.scripts.wake_watchdog.time.time", lambda: 10_000 + 181)
    assert _watching(str(inbox), [cmd]) is False


def test_spawn_channel_flag_is_off_unless_asked(monkeypatch):
    class Cell:
        role = "lin"

    monkeypatch.delenv("SWARPH_CHANNEL", raising=False)
    argv = _build_claude_argv(Cell(), "sid-960", True, [])
    assert "--channels" not in argv
    assert "--dangerously-load-development-channels" not in argv
    monkeypatch.setenv("SWARPH_CHANNEL", "allowlisted")
    argv = _build_claude_argv(Cell(), "sid-960", True, [])
    assert "--channels" in argv and "plugin:swarph@swarph" in argv
    monkeypatch.setenv("SWARPH_CHANNEL", "dev")
    argv = _build_claude_argv(Cell(), "sid-960", True, [])
    assert "--dangerously-load-development-channels" in argv


def test_later_dm_is_pushed_with_no_further_client_traffic(tmp_path):
    """Stdio server must push a DM appended after initialize, with stdin idle."""
    import subprocess
    import sys
    import threading

    root = tmp_path / "state"
    side = root / "cursor-lin" / "mesh-sidecar"
    side.mkdir(parents=True)
    inbox = side / "inbox.log"
    inbox.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["SWARPH_SELF"] = "cursor-lin"
    env["SWARPH_STATE"] = str(root)
    env["SWARPH_CHANNEL"] = "allowlisted"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarph_cli.channel"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    lines: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if "id=7 " in line:
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    }) + "\n")
    proc.stdin.flush()
    thread.join(1.5)
    inbox.write_text(_row(7, "cursor-lin", body="later") + "\n", encoding="utf-8")
    thread.join(5)
    proc.kill()
    proc.wait(timeout=5)
    assert any("id=7 " in line for line in lines), lines


def _server(root: Path, mode: str | None):
    import subprocess
    import sys

    env = os.environ.copy()
    env["SWARPH_SELF"] = "cursor-lin"
    env["SWARPH_STATE"] = str(root)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    if mode is None:
        env.pop("SWARPH_CHANNEL", None)
    else:
        env["SWARPH_CHANNEL"] = mode
    return subprocess.Popen(
        [sys.executable, "-m", "swarph_cli.channel"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def _init(proc) -> str:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    }) + "\n")
    proc.stdin.flush()
    return proc.stdout.readline()


def test_unset_channel_does_not_declare_or_write(tmp_path):
    root = tmp_path / "state"
    side = root / "cursor-lin" / "mesh-sidecar"
    side.mkdir(parents=True)
    inbox = side / "inbox.log"
    inbox.write_text("", encoding="utf-8")
    proc = _server(root, None)
    line = _init(proc)
    inbox.write_text(_row(8, "cursor-lin", body="gated") + "\n", encoding="utf-8")
    import time
    time.sleep(2)
    proc.kill()
    proc.wait(timeout=5)
    assert "claude/channel" not in line
    assert not (side / "channel_cursor.json").exists()
    assert not (side / "channel_heartbeat.json").exists()


def test_second_server_does_not_poll(tmp_path):
    import threading
    import time

    root = tmp_path / "state"
    side = root / "cursor-lin" / "mesh-sidecar"
    side.mkdir(parents=True)
    inbox = side / "inbox.log"
    inbox.write_text("", encoding="utf-8")
    first = _server(root, "allowlisted")
    second = _server(root, "allowlisted")
    caught: dict[str, list[str]] = {"a": [], "b": []}

    def read(proc, key: str) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            caught[key].append(line)
            if "id=11 " in line:
                return

    threads = [
        threading.Thread(target=read, args=(first, "a"), daemon=True),
        threading.Thread(target=read, args=(second, "b"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for proc in (first, second):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }) + "\n")
        proc.stdin.flush()
        time.sleep(0.4)
    inbox.write_text(_row(11, "cursor-lin", body="once") + "\n", encoding="utf-8")
    for thread in threads:
        thread.join(5)
    first.kill()
    second.kill()
    first.wait(timeout=5)
    second.wait(timeout=5)
    pushes = [
        key for key, lines in caught.items() if any("id=11 " in line for line in lines)
    ]
    assert len(pushes) == 1, caught


def test_unset_self_exits_nonzero(monkeypatch):
    import subprocess
    import sys
    env = os.environ.copy()
    env.pop("SWARPH_SELF", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.run(
        [sys.executable, "-c", "import swarph_cli.channel as c; raise SystemExit(c.main())"],
        input="", env=env, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "SWARPH_SELF" in proc.stderr
