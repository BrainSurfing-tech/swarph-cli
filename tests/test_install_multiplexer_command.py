"""Tests for ``swarph install-multiplexer`` — fetch the checksum-verified psmux
binary on Windows; on Linux/macOS it just points at the native tmux.
"""

from __future__ import annotations

import sys

import pytest

from swarph_cli.commands import install_multiplexer as im


def test_install_multiplexer_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS
    handler = _VERB_HANDLERS.get("install-multiplexer")
    if handler is None:
        pytest.skip("verb not yet registered by parent (main.py owned externally)")
    assert handler == "swarph_cli.commands.install_multiplexer.run_install_multiplexer"


def test_non_windows_prints_native_tmux_hint(monkeypatch, capsys):
    # FORCE the non-Windows branch regardless of host — CI runs on Windows too,
    # where the unforced verb would take the Windows path (host-dependent green).
    monkeypatch.setattr(im.sys, "platform", "linux")
    rc = im.run_install_multiplexer([])
    assert rc == 0
    err = capsys.readouterr().err.lower()
    assert "tmux" in err
    assert "apt" in err or "brew" in err


def test_windows_already_present_returns_0(monkeypatch, capsys):
    monkeypatch.setattr(im.sys, "platform", "win32")
    monkeypatch.setattr(im, "find_multiplexer", lambda: "C:/tools/tmux.exe")
    rc = im.run_install_multiplexer([])
    assert rc == 0
    assert "already present" in capsys.readouterr().out.lower()


def test_windows_installs_when_absent(monkeypatch, capsys):
    monkeypatch.setattr(im.sys, "platform", "win32")
    monkeypatch.setattr(im, "find_multiplexer", lambda: None)
    monkeypatch.setattr(im, "install_psmux",
                        lambda d, **kw: "C:/Users/x/.swarph/bin/tmux.exe")
    rc = im.run_install_multiplexer([])
    assert rc == 0
    assert "installed" in capsys.readouterr().out.lower()


def test_windows_force_reinstalls_even_if_present(monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr(im.sys, "platform", "win32")
    monkeypatch.setattr(im, "find_multiplexer", lambda: "C:/tools/tmux.exe")

    def fake_install(d, **kw):
        calls["hit"] = True
        return "C:/Users/x/.swarph/bin/tmux.exe"

    monkeypatch.setattr(im, "install_psmux", fake_install)
    rc = im.run_install_multiplexer(["--force"])
    assert rc == 0
    assert calls.get("hit") is True
    assert "installed" in capsys.readouterr().out.lower()


def test_windows_install_error_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(im.sys, "platform", "win32")
    monkeypatch.setattr(im, "find_multiplexer", lambda: None)

    def boom(d, **kw):
        raise RuntimeError("psmux checksum mismatch")

    monkeypatch.setattr(im, "install_psmux", boom)
    rc = im.run_install_multiplexer([])
    assert rc == 1
    assert "checksum mismatch" in capsys.readouterr().err.lower()
