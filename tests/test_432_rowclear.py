"""Card #432 — the monitor types /clear at a row boundary, once.

v1 opt-in is drop-on-meta-edge. SWARPH_ROWCLEAR=off|shadow|live.
A live row, a busy pane, or an owed DM never gets a key. Shadow writes
one would-clear record and types nothing. Live types exactly '/clear'
once per idle boundary. SessionStart source=clear rewrites the role's
session pin, and spawn resumes that id.

>>> RED on main d1f7324: `_maybe_rowclear` does not exist, so every test
fails the absent-symbol assert. The head must keep the safety asserts
(zero keys) true — a path that types /clear into a live row is the FAIL.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from swarph_cli.cell import session_state_path
from swarph_cli.commands import spawn
from swarph_cli.commands import mesh
from swarph_shared.cell import Cell


class TmuxSink:
    keeps_ledger = True

    def __init__(self, target: str):
        self.target = target
        self.name = f"tmux:{target}"


class _State:
    def __init__(self, tmp_path: Path, *, name="drop-on-meta-edge",
                 delivered=5, observed=5, wake_outstanding=False):
        self.self_name = name
        self.state_dir = tmp_path
        self.gateway = "http://gw:8788"
        self.token = "tok"
        self.sinks = [TmuxSink("drop-on-meta-edge")]
        self.observed = {"last_msg_id": observed}
        self._led = {
            "tmux:drop-on-meta-edge": {
                "last_delivered_id": delivered,
                "last_delivery_at": 0.0,
                "consecutive_failures": 0,
                "wake_outstanding": wake_outstanding,
            }
        }

    def ledger(self, name: str) -> dict:
        return self._led[name]


def _arm(monkeypatch, state, *, rows=0, pane=False, mode="live", statuses=None):
    """rows is an int count of open rows, or None for an unreadable board.

    statuses, when given, is the status of each returned row (a
    fallback_fired row, an unknown status).
    """
    monkeypatch.setenv("SWARPH_ROWCLEAR", mode)
    typed: list[str] = []

    def type_clear(target: str) -> bool:
        typed.append("/clear")
        return True

    monkeypatch.setattr(mesh, "_type_slash_clear", type_clear, raising=False)
    monkeypatch.setattr(mesh, "_agent_running", lambda t: pane)
    row_statuses = list(statuses) if statuses is not None else (
        [] if rows is None else ["open"] * rows)

    def http_get(url, token, timeout=10.0):
        if rows is None:
            return 500, {"detail": "down"}
        body = {"obligations": [
            {"id": i + 1, "holder": state.self_name, "status": s}
            for i, s in enumerate(row_statuses)
        ]}
        return 200, body

    monkeypatch.setattr(mesh, "_http_get_json", http_get)
    return typed


def _run(state) -> str:
    assert hasattr(mesh, "_maybe_rowclear"), (
        "card #432 rowclear is absent on this head")
    return mesh._maybe_rowclear(state)


def test_a_cell_with_a_live_row_is_never_cleared(monkeypatch, tmp_path):
    state = _State(tmp_path)
    typed = _arm(monkeypatch, state, rows=1, pane=False, mode="live")
    assert _run(state) == "blocked-live-row"
    assert typed == []
    assert _run(state) == "blocked-live-row"
    assert typed == [], "a second poll still types nothing"


def test_a_busy_pane_is_never_cleared(monkeypatch, tmp_path):
    state = _State(tmp_path)
    typed = _arm(monkeypatch, state, rows=0, pane=True, mode="live")
    assert _run(state) == "blocked-busy"
    assert typed == []


def test_an_unknown_pane_is_never_cleared(monkeypatch, tmp_path):
    state = _State(tmp_path)
    typed = _arm(monkeypatch, state, rows=0, pane=None, mode="live")
    assert _run(state) == "blocked-unknown-pane"
    assert typed == []


def test_an_owed_dm_blocks_the_clear(monkeypatch, tmp_path):
    state = _State(tmp_path, delivered=4, observed=5)
    typed = _arm(monkeypatch, state, rows=0, pane=False, mode="live")
    assert _run(state) == "blocked-owed-dm"
    assert typed == []


def test_an_outstanding_wake_blocks_the_clear(monkeypatch, tmp_path):
    state = _State(tmp_path, wake_outstanding=True)
    typed = _arm(monkeypatch, state, rows=0, pane=False, mode="live")
    assert _run(state) == "blocked-owed-dm"
    assert typed == []


def test_unreadable_rows_block_the_clear(monkeypatch, tmp_path):
    state = _State(tmp_path)
    typed = _arm(monkeypatch, state, rows=None, pane=False, mode="live")
    assert _run(state) == "blocked-rows-unknown"
    assert typed == []


def test_a_fallback_fired_row_does_not_block(monkeypatch, tmp_path):
    """fallback_fired is finished. The opted-in cell holds one, and it
    must not freeze the clear forever."""
    state = _State(tmp_path)
    typed = _arm(
        monkeypatch, state, pane=False, mode="live",
        statuses=["fallback_fired", "closed"],
    )
    assert _run(state) == "live"
    assert typed == ["/clear"]


def test_an_unknown_status_fails_closed(monkeypatch, tmp_path):
    """A status outside open / closed / fallback_fired is not an empty cell."""
    state = _State(tmp_path)
    typed = _arm(
        monkeypatch, state, pane=False, mode="live",
        statuses=["closed", "mystery"],
    )
    assert _run(state) == "blocked-rows-unknown"
    assert typed == []
    assert not (tmp_path / "rowclear.json").exists()


def test_shadow_types_nothing_and_writes_one_would_clear(monkeypatch, tmp_path):
    state = _State(tmp_path)
    typed = _arm(monkeypatch, state, rows=0, pane=False, mode="shadow")
    assert _run(state) == "shadow"
    assert typed == []
    lines = (tmp_path / "rowclear.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["cell"] == "drop-on-meta-edge"
    assert rec["action"] == "would-clear"
    assert rec["open_rows"] == 0
    assert rec["pane_state"] == "idle"
    assert rec["time"]
    # the same idle boundary does not log a second record
    assert _run(state) == "latched"
    assert typed == []
    again = (tmp_path / "rowclear.log").read_text(encoding="utf-8").splitlines()
    assert len(again) == 1


def test_live_types_clear_once_per_idle_boundary(monkeypatch, tmp_path):
    state = _State(tmp_path)
    typed = _arm(monkeypatch, state, rows=0, pane=False, mode="live")
    assert _run(state) == "live"
    assert typed == ["/clear"]
    assert _run(state) == "latched"
    assert typed == ["/clear"], "a second poll on the same boundary types nothing"
    # work resumes (a row), then the pane is idle and empty again: one more
    typed2 = _arm(monkeypatch, state, rows=1, pane=False, mode="live")
    assert _run(state) == "blocked-live-row"
    assert typed2 == []
    typed3 = _arm(monkeypatch, state, rows=0, pane=False, mode="live")
    assert _run(state) == "live"
    assert typed3 == ["/clear"]


def test_off_and_non_listed_cells_are_unchanged(monkeypatch, tmp_path):
    listed = _State(tmp_path / "off")
    typed = _arm(monkeypatch, listed, rows=0, pane=False, mode="off")
    assert _run(listed) == "off"
    assert typed == []
    assert not (listed.state_dir / "rowclear.log").exists()
    assert not (listed.state_dir / "rowclear.json").exists()

    other = _State(tmp_path / "other", name="science-claude")
    typed_other = _arm(monkeypatch, other, rows=0, pane=False, mode="live")
    assert _run(other) == "not-opted-in"
    assert typed_other == []
    assert not (other.state_dir / "rowclear.log").exists()


def _cell(tmp_path: Path, role: str) -> Cell:
    cwd = tmp_path / "work"
    cwd.mkdir()
    cell = Cell(
        schema_version="1", name=role, role=role, cwd=cwd,
        provider="claude", session_id=None, starter_prompt_path=None, extra={},
    )
    cell.source_path = tmp_path / f"{role}.yaml"
    return cell


def test_sessionstart_clear_rewrites_the_pin_and_spawn_resumes_it(
        monkeypatch, tmp_path):
    from swarph_cli.rowclear import apply_sessionstart_clear

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    role = "drop-on-meta-edge"
    cell = _cell(tmp_path, role)
    old = str(uuid.uuid4())
    new = str(uuid.uuid4())
    from swarph_cli.cell import _write_session_sidecar
    _write_session_sidecar(session_state_path(role), old, cell.cwd)

    assert apply_sessionstart_clear(
        {"source": "resume", "session_id": new, "cwd": str(cell.cwd)}, role,
    ) is False
    assert session_state_path(role).read_text(encoding="utf-8").splitlines()[0] == old

    assert apply_sessionstart_clear(
        {"source": "clear", "session_id": new, "cwd": str(cell.cwd)}, role,
    ) is True
    pin = session_state_path(role).read_text(encoding="utf-8").splitlines()
    assert pin[0] == new

    from swarph_cli.cell import load_or_create_session_id
    sid, generated, effective = load_or_create_session_id(role, cell)
    assert sid == new and generated is False and effective == role

    monkeypatch.setattr(spawn, "_session_state_exists", lambda s: True)
    argv = spawn._build_claude_argv(cell, sid, True, [])
    assert argv[argv.index("--resume") + 1] == new
