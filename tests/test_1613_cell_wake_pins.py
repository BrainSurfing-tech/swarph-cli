"""Card #1613 (obligation #5493): three cell_wake pins from plan-review 5487.

Follow-up to card #1612 / PR 738 (tests/test_1612_cell_wake_hardening.py).
Each pin kills one mutant that PR 738's own tests leave alive:
  (a) bool id accepted (isinstance instead of strict type),
  (b) fast path without the ctime check,
  (c) fast path taken on a changed inode.
Assertions preserved from the drop file; only the POSIX guard wiring
follows test_1612 so no CI leg errors at collection (pwd is POSIX-only).
"""
import json
import os
import time
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
    return SimpleNamespace(inbox=inbox, state=state, wakes=wakes,
                           args=["--cell", "test", "--inbox", str(inbox), "--pane", "test:0.0", "--once"])


def _cursor(ctx):
    return json.loads((ctx.state / "cursor.json").read_text())


def test_bool_id_as_first_record_is_corrupt_and_cursor_stays_loadable(wake_env):
    # The PR's bool case appends {"id": true} after id 1, where True == 1 hides an accepted bool.
    wake_env.inbox.write_bytes(b'{"id": true, "content": "x"}\n')
    assert cell_wake.main(wake_env.args) == 0
    assert _cursor(wake_env)["seen_ids"] == []
    assert cell_wake.main(wake_env.args) == 0  # a stored `true` would make the cursor invalid: exit 2
    with wake_env.inbox.open("ab") as out:
        out.write(b'{"id": 1}\n')
    assert cell_wake.main(wake_env.args) == 0
    assert _cursor(wake_env)["seen_ids"] == [1]


def test_same_size_rewrite_with_mtime_put_back_is_noticed(wake_env):
    wake_env.inbox.write_bytes(b'{"id": 1}\n{"id": 2}\n')
    assert cell_wake.main(wake_env.args) == 0
    assert len(wake_env.wakes) == 1
    before = wake_env.inbox.stat()
    time.sleep(0.05)  # a coarse-timestamp kernel needs a later tick for ctime to move
    fd = os.open(wake_env.inbox, os.O_WRONLY)
    os.pwrite(fd, b'{"id": 3}\n{"id": 4}\n', 0)
    os.close(fd)
    os.utime(wake_env.inbox, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = wake_env.inbox.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert cell_wake.main(wake_env.args) == 0
    assert len(wake_env.wakes) == 2  # only ctime told the two files apart
    assert {3, 4} <= set(_cursor(wake_env)["seen_ids"])


def test_changed_inode_never_takes_the_idle_fast_path(wake_env):
    wake_env.inbox.write_bytes(b'{"id": 1}\n')
    assert cell_wake.main(wake_env.args) == 0
    assert len(wake_env.wakes) == 1
    cursor = _cursor(wake_env)
    cursor["inode"] += 1  # same size and timestamps, another file
    (wake_env.state / "cursor.json").write_text(json.dumps(cursor))
    assert cell_wake.main(wake_env.args) == 0
    assert len(wake_env.wakes) == 2  # rotation: one standing wake
    assert _cursor(wake_env)["inode"] == wake_env.inbox.stat().st_ino
