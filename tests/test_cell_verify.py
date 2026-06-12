# tests/test_cell_verify.py
from pathlib import Path

import pytest

from swarph_cli.capture import verify, manifest
from swarph_shared.cell import Cell


def _cell(cwd: Path) -> Cell:
    c = Cell(schema_version="1", name="droplet", role="droplet",
             cwd=cwd, provider="claude", session_id=None,
             starter_prompt_path=None, extra={})
    return c


def test_expected_project_dir_sanitizes_slashes():
    assert verify.expected_project_dir(Path("/root/hedge-fund-mcp")) == "-root-hedge-fund-mcp"


def test_no_pin_is_ok_fresh_genesis(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(tmp_path))
    monkeypatch.setattr(verify, "_read_pin", lambda role: (None, None))
    r = verify.verify_cell("droplet")
    assert r.ok and r.code == 0


def test_no_jsonl_anywhere_is_ok_unstarted_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(tmp_path))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(tmp_path)))
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [])
    r = verify.verify_cell("droplet")
    assert r.ok and r.code == 0


def test_jsonl_under_wrong_project_dir_is_cwd_drift(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"
    cwd.mkdir()
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(cwd))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    wrong = tmp_path / ".claude" / "projects" / "-some-other-dir" / "uuid-1.jsonl"
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [wrong])
    r = verify.verify_cell("droplet")
    assert not r.ok and r.code == 3


def test_jsonl_under_correct_dir_and_holder_dead_clears_and_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"
    cwd.mkdir()
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(cwd))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    good = Path(".claude/projects") / verify.expected_project_dir(cwd) / "uuid-1.jsonl"
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [good])
    manifest.write_manifest("droplet", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1", live_pin_holder="droplet")
    monkeypatch.setattr(verify, "probe_holder_liveness", lambda h: False)  # dead holder
    r = verify.verify_cell("droplet")
    assert r.ok and r.code == 0
    # the stale poison-pin was cleared
    assert manifest.read_manifest("droplet")["head"]["live_pin_holder"] is None


def test_holder_alive_refuses_double_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"
    cwd.mkdir()
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(cwd))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    good = Path(".claude/projects") / verify.expected_project_dir(cwd) / "uuid-1.jsonl"
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [good])
    manifest.write_manifest("droplet", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1", live_pin_holder="droplet")
    monkeypatch.setattr(verify, "probe_holder_liveness", lambda h: True)  # ALIVE
    r = verify.verify_cell("droplet")
    assert not r.ok and r.code == 4
    # a real live holder's pin is NOT cleared
    assert manifest.read_manifest("droplet")["head"]["live_pin_holder"] == "droplet"


def test_cross_name_live_holder_refuses_renamed_cell_incident(tmp_path, monkeypatch):
    # THE incident: drop-mother + droplet both pin uuid-1. drop-mother is live
    # under its own tmux; verify of *droplet* must still refuse — the footgun
    # is per-UUID, not per-role.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"
    cwd.mkdir()
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(cwd))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    good = Path(".claude/projects") / verify.expected_project_dir(cwd) / "uuid-1.jsonl"
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [good])
    # droplet's OWN manifest is clean; drop-mother's holds the live pin
    manifest.write_manifest("droplet", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1")
    manifest.write_manifest("drop-mother", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1",
                            live_pin_holder="drop-mother")
    monkeypatch.setattr(verify, "probe_holder_liveness", lambda h: True)
    r = verify.verify_cell("droplet")
    assert not r.ok and r.code == 4
    assert "drop-mother" in r.reason


def test_cross_name_dead_holder_cleared_and_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"
    cwd.mkdir()
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(cwd))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [])
    manifest.write_manifest("drop-mother", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1",
                            live_pin_holder="drop-mother")
    monkeypatch.setattr(verify, "probe_holder_liveness", lambda h: False)
    r = verify.verify_cell("droplet")
    assert r.ok and r.code == 0
    # the OTHER role's stale poison-pin got cleared
    assert manifest.read_manifest("drop-mother")["head"]["live_pin_holder"] is None
