"""Card #1612 (obligation #5485): cell_wake B1 hardening pins.

Items:
 (1) O7 corrupt DM id (string/null/float/bool/negative/zero) skipped.
 (2) Any per-record exception (incl. RecursionError, 100k-deep line) corrupt.
 (3) O2: a corrupt record raises exactly ONE standing wake (pin).
 (4) Heartbeat written only AFTER a completed poll.
 (5) Idle poll does not rehash the consumed prefix.
"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

if os.name != "posix":
    pytest.skip("cell-side wake requires POSIX", allow_module_level=True)

import pwd

from swarph_cli import cell_wake


@pytest.fixture
def wake_env(monkeypatch, tmp_path):
    inbox = tmp_path / "proxy" / "inbox.log"
    inbox.parent.mkdir()
    inbox.write_bytes(b"")
    state = tmp_path / "state"
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setattr(os, "geteuid", lambda: 888)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="cell-test"))
    wakes = []
    monkeypatch.setattr(cell_wake, "_wake", lambda pane: (wakes.append(pane), True)[1])
    ctx = SimpleNamespace(inbox=inbox, state=state, wakes=wakes,
                          args=["--cell", "test", "--inbox", str(inbox),
                                "--pane", "test:0.0", "--once"])
    return ctx


def _cursor(ctx):
    return json.loads((ctx.state / "cursor.json").read_text())


def _append(ctx, raw):
    with ctx.inbox.open("ab") as fh:
        fh.write(raw if raw.endswith(b"\n") else raw + b"\n")


# (1) -----------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    b'{"id": "7", "content": "x"}',
    b'{"id": null, "content": "x"}',
    b'{"id": 3.5, "content": "x"}',
    b'{"id": true, "content": "x"}',
    b'{"id": -4, "content": "x"}',
    b'{"id": 0, "content": "x"}',
])
def test_corrupt_id_skipped_cursor_advances_later_valid_wakes(wake_env, bad):
    _append(wake_env, b'{"id": 1, "content": "a"}')
    assert cell_wake.main(wake_env.args) == 0
    assert _cursor(wake_env)["seen_ids"] == [1]
    _append(wake_env, bad)
    assert cell_wake.main(wake_env.args) == 0  # never crashes
    cur = _cursor(wake_env)
    assert cur["offset"] == wake_env.inbox.stat().st_size  # cursor advances
    assert cur["seen_ids"] == [1]  # corrupt id not recorded
    _append(wake_env, b'{"id": 2, "content": "b"}')
    assert cell_wake.main(wake_env.args) == 0
    assert _cursor(wake_env)["seen_ids"] == [1, 2]


# (2) -----------------------------------------------------------------------
def test_deep_nesting_100k_line_is_corrupt_and_loop_continues(wake_env, capsys):
    _append(wake_env, b'{"id": 1}')
    assert cell_wake.main(wake_env.args) == 0
    _append(wake_env, b"[" * 100000 + b"]" * 100000)
    assert cell_wake.main(wake_env.args) == 0
    assert _cursor(wake_env)["offset"] == wake_env.inbox.stat().st_size
    assert _cursor(wake_env)["seen_ids"] == [1]
    _append(wake_env, b'{"id": 2}')
    assert cell_wake.main(wake_env.args) == 0
    assert _cursor(wake_env)["seen_ids"] == [1, 2]


@pytest.mark.parametrize("exc_type", [RecursionError, RuntimeError])
def test_any_per_record_exception_is_corrupt_logged_skipped(wake_env, monkeypatch, capsys, exc_type):
    _append(wake_env, b'{"id": 1}')
    assert cell_wake.main(wake_env.args) == 0
    real_loads = json.loads

    def exploding(data, *a, **kw):
        if b"explode" in (data if isinstance(data, (bytes, bytearray)) else str(data).encode()):
            raise exc_type("simulated per-record failure")
        return real_loads(data, *a, **kw)

    monkeypatch.setattr(json, "loads", exploding)
    _append(wake_env, b'{"id": 99, "content": "explode"}')
    assert cell_wake.main(wake_env.args) == 0
    assert "corrupt" in capsys.readouterr().err.lower()
    cur = _cursor(wake_env)
    assert cur["offset"] == wake_env.inbox.stat().st_size
    assert cur["seen_ids"] == [1]
    monkeypatch.setattr(json, "loads", real_loads)
    _append(wake_env, b'{"id": 2}')
    assert cell_wake.main(wake_env.args) == 0
    assert _cursor(wake_env)["seen_ids"] == [1, 2]


# (3) -----------------------------------------------------------------------
def test_corrupt_record_raises_exactly_one_standing_wake(wake_env):
    _append(wake_env, b'{"id": "bad", "content": "x"}')
    assert cell_wake.main(wake_env.args) == 0
    assert len(wake_env.wakes) == 1
    assert cell_wake.main(wake_env.args) == 0
    assert len(wake_env.wakes) == 1
    assert cell_wake.main(wake_env.args) == 0
    assert len(wake_env.wakes) == 1


# (4) -----------------------------------------------------------------------
def test_heartbeat_not_written_when_inbox_read_crashes(wake_env, monkeypatch):
    _append(wake_env, b'{"id": 1}')
    assert cell_wake.main(wake_env.args) == 0
    hb = wake_env.state / "heartbeat.json"
    assert hb.is_file()
    sentinel = b'{"at": 1.0}'
    hb.write_bytes(sentinel)

    def crash(inbox, cursor):
        raise OSError("simulated inbox read crash")

    monkeypatch.setattr(cell_wake, "_read_batch", crash)
    assert cell_wake.main(wake_env.args) == 2
    assert hb.read_bytes() == sentinel


# (5) -----------------------------------------------------------------------
def test_idle_poll_does_not_rehash_consumed_prefix(wake_env, monkeypatch):
    # A sizeable consumed prefix; content irrelevant beyond valid ids.
    lines = b"".join(
        json.dumps({"id": i, "content": "p" * 200}).encode() + b"\n"
        for i in range(1, 501))
    wake_env.inbox.write_bytes(lines)
    assert cell_wake.main(wake_env.args) == 0
    size = wake_env.inbox.stat().st_size
    assert _cursor(wake_env)["offset"] == size

    real_sha256 = hashlib.sha256
    hashed = []

    class CountingHash:
        def __init__(self, data=b""):
            self._h = real_sha256(data)
            if data:
                hashed.append(len(data))

        def update(self, data=b""):
            hashed.append(len(data))
            return self._h.update(data)

        def hexdigest(self):
            return self._h.hexdigest()

        def digest(self):
            return self._h.digest()

        def copy(self):
            return self._h.copy()

    monkeypatch.setattr(hashlib, "sha256", CountingHash)
    wakes_before = len(wake_env.wakes)
    assert cell_wake.main(wake_env.args) == 0
    assert len(wake_env.wakes) == wakes_before  # idle: no wake
    assert sum(hashed) == 0, f"idle poll rehashed {sum(hashed)} bytes of {size}"


def test_legacy_cursor_without_idle_metadata_still_loads(wake_env):
    _append(wake_env, b'{"id": 1}')
    assert cell_wake.main(wake_env.args) == 0
    cur = _cursor(wake_env)
    legacy = {k: cur[k] for k in ("device", "inode", "offset", "sha256", "seen_ids")}
    (wake_env.state / "cursor.json").write_text(json.dumps(legacy))
    loaded = cell_wake._load_cursor(wake_env.state / "cursor.json")
    assert loaded["seen_ids"] == [1]
    assert cell_wake.main(wake_env.args) == 0
