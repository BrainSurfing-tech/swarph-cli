"""Provider-non-discriminatory tmux launch: the named-tmux branch lives in the
BASE ProviderMembrane.pre_launch, so every membrane (claude/codex/antigravity/
grok) gets a session when spawned with a name. Claude keeps only its extra WT
relaunch. These tests pin the membrane DISPATCH by mocking _launch_via_tmux and
_relaunch_in_windows_terminal — they do NOT exercise _launch_via_tmux internals
(that's tests/test_spawn_tmux_session.py)."""
from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

from swarph_cli.commands import spawn

BIN = "/usr/bin/claude"
ARGV = ["claude", "--name", "lab"]


def _cell():
    return types.SimpleNamespace(cwd=Path("/home/ubuntu/lab"))


def _call(membrane, monkeypatch, *, session_name, tmux_ok=True, wt_ok=False):
    launch = MagicMock(return_value=tmux_ok)
    wt = MagicMock(return_value=wt_ok)
    monkeypatch.setattr(spawn, "_launch_via_tmux", launch)
    monkeypatch.setattr(spawn, "_relaunch_in_windows_terminal", wt)
    rc = membrane.pre_launch(_cell(), BIN, ARGV, no_banner=True, session_name=session_name)
    return rc, launch, wt


def test_base_launches_tmux_when_named(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
    assert rc == 0
    launch.assert_called_once_with(BIN, ARGV, Path("/home/ubuntu/lab"), "lab")


def test_base_returns_none_when_unnamed(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name=None)
    assert rc is None
    launch.assert_not_called()


def test_base_returns_none_when_tmux_declines(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=False)
    assert rc is None
    launch.assert_called_once()


def test_claude_takes_base_tmux_without_reaching_wt(monkeypatch):
    m = spawn.MEMBRANES["claude"]
    rc, launch, wt = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
    assert rc == 0
    launch.assert_called_once()
    wt.assert_not_called()          # base took over → Claude's WT path not reached


def test_claude_reaches_wt_when_base_declines(monkeypatch):
    m = spawn.MEMBRANES["claude"]
    rc, launch, wt = _call(m, monkeypatch, session_name="lab", tmux_ok=False, wt_ok=True)
    assert rc == 0                  # WT relaunch took over
    wt.assert_called_once()


def test_grok_has_no_own_pre_launch():
    # Grok's override is deleted → resolves to the base implementation.
    assert spawn.GrokMembrane.pre_launch is spawn.ProviderMembrane.pre_launch


def test_codex_and_antigravity_inherit_base_tmux(monkeypatch):
    for key in ("codex", "antigravity"):
        m = spawn.MEMBRANES[key]
        assert type(m).pre_launch is spawn.ProviderMembrane.pre_launch
        rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
        assert rc == 0, key
        launch.assert_called_once()
