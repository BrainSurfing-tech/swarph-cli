"""Spawn-side live-pin recording (feeds the verify gate's double-resume probe)."""
from pathlib import Path

import pytest

from swarph_cli.commands import spawn
from swarph_cli.capture import manifest


def test_sets_holder_when_inside_tmux(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    manifest.write_manifest("droplet", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1")
    monkeypatch.setattr(spawn, "_current_tmux_session", lambda: "droplet")
    spawn._set_live_pin_safe("droplet")
    assert manifest.read_manifest("droplet")["head"]["live_pin_holder"] == "droplet"


def test_console_spawn_is_untracked(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    manifest.write_manifest("droplet", recipe="r", pin="p", service="s",
                            lineage="l", session_id="uuid-1")
    monkeypatch.setattr(spawn, "_current_tmux_session", lambda: None)
    spawn._set_live_pin_safe("droplet")
    assert manifest.read_manifest("droplet")["head"]["live_pin_holder"] is None


def test_unhardened_cell_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(spawn, "_current_tmux_session", lambda: "x")
    spawn._set_live_pin_safe("ghost")  # no manifest → silently skipped
    assert manifest.read_manifest("ghost") is None


def test_failure_never_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(spawn, "_current_tmux_session",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    spawn._set_live_pin_safe("droplet")  # must swallow + warn
    assert "non-fatal" in capsys.readouterr().err


def test_current_tmux_session_none_outside_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert spawn._current_tmux_session() is None
