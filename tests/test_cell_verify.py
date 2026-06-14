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


def test_live_unhardened_cell_warns_but_allows(tmp_path, monkeypatch):
    # science-claude's co-review case (mesh #2811): a cell that is demonstrably
    # alive but was hand-deployed (never `harden`ed) has NO capture manifest, so
    # the per-UUID liveness sweep finds nothing to probe. The pin is untouched
    # (good) but verify used to pass MUTE — violating its own fail-LOUD contract.
    # It must ALLOW (no manifest = no proof of double-resume) yet WARN loudly.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"
    cwd.mkdir()
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(cwd))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    good = Path(".claude/projects") / verify.expected_project_dir(cwd) / "uuid-1.jsonl"
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [good])
    # NO manifest written for this role -> un-hardened, unprotected
    r = verify.verify_cell("science-claude")
    assert r.ok and r.code == 0
    assert r.warnings, "an un-hardened live cell must emit a loud warning, not pass mute"
    assert any("harden" in w for w in r.warnings)


def test_hardened_cell_emits_no_unprotected_warning(tmp_path, monkeypatch):
    # The inverse: a cell WITH a manifest is protected -> no unprotected warning.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"
    cwd.mkdir()
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(cwd))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    good = Path(".claude/projects") / verify.expected_project_dir(cwd) / "uuid-1.jsonl"
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [good])
    manifest.write_manifest("droplet", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1", live_pin_holder="droplet")
    monkeypatch.setattr(verify, "probe_holder_liveness", lambda h: False)
    r = verify.verify_cell("droplet")
    assert r.ok and r.code == 0
    assert not r.warnings


def test_unstarted_pin_no_jsonl_does_not_warn(tmp_path, monkeypatch):
    # A pin minted but never run (no jsonl) is benign fresh state, not an
    # unprotected live cell -> no noise.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(tmp_path))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(tmp_path)))
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [])
    r = verify.verify_cell("science-claude")
    assert r.ok and r.code == 0
    assert not r.warnings


def test_corrupt_own_manifest_fails_closed_not_throws(tmp_path, monkeypatch):
    # science-claude defensive co-review (mesh #2826): read_manifest RAISES on a
    # corrupt manifest (json.loads). The un-hardened check must NOT parse — it
    # runs in the @.service ExecStart gate, so a throw there = no spawn. A corrupt
    # OWN manifest must route to the existing fail-CLOSED handling (code 5), never
    # an unhandled exception, AND must not emit the "no manifest" warning (a
    # manifest FILE exists — it's just damaged, not absent).
    from swarph_cli.capture import paths as _paths
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "right"
    cwd.mkdir()
    monkeypatch.setattr(verify, "_resolve_cell", lambda role: _cell(cwd))
    monkeypatch.setattr(verify, "_read_pin", lambda role: ("uuid-1", str(cwd)))
    good = Path(".claude/projects") / verify.expected_project_dir(cwd) / "uuid-1.jsonl"
    monkeypatch.setattr(verify, "locate_session_jsonl", lambda u: [good])
    mpath = _paths.manifest_path("science-claude")
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text("{ this is not valid json", encoding="utf-8")
    r = verify.verify_cell("science-claude")  # MUST NOT raise
    assert not r.ok and r.code == 5
    assert not any("no capture manifest" in w for w in r.warnings)
