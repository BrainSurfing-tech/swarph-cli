"""#1686: `swarph monitor install-unit` on macOS renders the launchd agent.

The darwin path is forced through the module's platform seam, so these
tests run on every platform (no skipif(darwin)): on macOS the seam is
the real sys.platform, elsewhere the tests pin it. A test that only
runs where it cannot fail is not a test.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

import swarph_cli.commands.monitor as monitor

LABEL = "tech.brainsurfing.swarph.monitor.smoke-peer"


@pytest.fixture
def darwin(monkeypatch):
    monkeypatch.setattr(monitor, "_install_platform", lambda: "darwin")


def test_prints_plist_with_no_placeholders_left(darwin, capsys):
    rc = monitor.run_monitor(
        ["install-unit", "--as", "smoke-peer",
         "--gateway", "http://100.64.189.91:8788",
         "--home", "/Users/smoke"])
    out = capsys.readouterr().out
    assert rc == 0
    assert not re.findall(r"__[A-Z_]+__", out), "unsubstituted placeholder survived"
    doc = plistlib.loads(out.encode())
    assert doc["Label"] == LABEL
    assert doc["ProgramArguments"] == [
        "/Users/smoke/.local/bin/swarph", "monitor", "start",
        "--as", "smoke-peer", "--deliver", "pull", "--foreground",
    ]
    assert doc["RunAtLoad"] is True and doc["KeepAlive"] is True
    assert doc["EnvironmentVariables"]["HOME"] == "/Users/smoke"
    assert doc["EnvironmentVariables"]["SWARPH_SELF"] == "smoke-peer"
    assert doc["EnvironmentVariables"]["MESH_GATEWAY_URL"] == \
        "http://100.64.189.91:8788"


def test_peer_is_required_for_the_label(darwin, capsys):
    rc = monitor.run_monitor(
        ["install-unit", "--gateway", "http://100.64.189.91:8788"])
    assert rc == 2
    assert "--as" in capsys.readouterr().err


def test_write_creates_only_the_launchagent_in_scratch_home(darwin, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc = monitor.run_monitor(
        ["install-unit", "--as", "smoke-peer", "--write",
         "--gateway", "http://127.0.0.1:8788", "--home", str(home)])
    assert rc == 0
    files = sorted(p for p in home.rglob("*") if p.is_file())
    assert files == [home / "Library" / "LaunchAgents" / f"{LABEL}.plist"]
    doc = plistlib.loads(files[0].read_bytes())
    assert doc["Label"] == LABEL
    assert str(home) in doc["EnvironmentVariables"]["HOME"]


BAD_PEERS = [
    "x/../../../../outside/evil", "a/b", "/abs", "a&b", "a<b",
    "p</string><string>--deliver</string><string>tmux:evil",
    "p</string></array><key>ProgramArguments</key><array><string>/bin/sh",
    "a b", "..", "-rf", "a" * 300, "abc\n", "smoke-peer\n",
    "smoke-peer ", "smoke-peer\t", "smoke-peer\r", " smoke-peer",
    "Smoke-Peer",
]


@pytest.mark.parametrize("bad", BAD_PEERS)
def test_bad_peer_names_are_refused_before_any_path_is_built(
        darwin, tmp_path, capsys, bad):
    home = tmp_path / "home"
    home.mkdir()
    rc = monitor.run_monitor(
        ["install-unit", f"--as={bad}",
         "--gateway", "http://100.64.189.91:8788", "--home", str(home)])
    captured = capsys.readouterr()
    assert rc == 2
    assert len(captured.err.strip().splitlines()) == 1
    rc = monitor.run_monitor(
        ["install-unit", f"--as={bad}", "--write",
         "--gateway", "http://100.64.189.91:8788", "--home", str(home)])
    assert rc == 2
    assert list(home.rglob("*")) == [], "a refused name created something"


def test_awkward_values_still_parse_as_plist(darwin, capsys):
    rc = monitor.run_monitor(
        ["install-unit", "--as", "smoke-peer",
         "--gateway", "http://gw.invalid/?a=1&b=2",
         "--home", "/Users/a&b", "--swarph-bin", "/opt/a<b/swarph"])
    out = capsys.readouterr().out
    assert rc == 0
    doc = plistlib.loads(out.encode())
    assert doc["EnvironmentVariables"]["HOME"] == "/Users/a&b"
    assert doc["EnvironmentVariables"]["MESH_GATEWAY_URL"] == \
        "http://gw.invalid/?a=1&b=2"
    assert doc["ProgramArguments"][0] == "/opt/a<b/swarph"


def test_write_mode_starts_no_process(darwin, tmp_path, monkeypatch):
    import os
    import subprocess

    started = []

    def recorder(name):
        def fake(*a, **k):
            started.append(name)
            raise AssertionError(f"install-unit started a process: {name}")
        return fake

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, recorder(name))
    for name in ("system", "execv", "execve", "execvp",
                 "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, recorder(name))
    home = tmp_path / "home"
    home.mkdir()
    rc = monitor.run_monitor(
        ["install-unit", "--as", "smoke-peer", "--write",
         "--gateway", "http://127.0.0.1:8788", "--home", str(home)])
    assert rc == 0
    assert started == []
    assert (home / "Library" / "LaunchAgents" / f"{LABEL}.plist").is_file()


def test_print_mode_writes_no_file(darwin, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    rc = monitor.run_monitor(
        ["install-unit", "--as", "smoke-peer",
         "--gateway", "http://127.0.0.1:8788", "--home", str(home)])
    assert rc == 0
    capsys.readouterr()
    assert [p for p in home.rglob("*") if p.is_file()] == []


def test_leftover_placeholder_in_a_value_is_refused(darwin, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc = monitor.run_monitor(
        ["install-unit", "--as", "smoke-peer", "--write",
         "--gateway", "http://gw.invalid/__PEER__", "--home", str(home)])
    assert rc == 2
    assert [p for p in home.rglob("*") if p.is_file()] == []


def test_print_path_names_the_plist_template_on_darwin(darwin, capsys):
    rc = monitor.run_monitor(["install-unit", "--print-path"])
    assert rc == 0
    assert Path(capsys.readouterr().out.strip()).name == "swarph-monitor.plist"


def test_unpinned_platform_seam_reaches_darwin_branch(capsys):
    # No monkeypatch: on a real Mac this runs the darwin branch end to
    # end; everywhere else it pins that the seam answers the real
    # platform. macos.yml's junit floor names this test, so on macOS it
    # must execute, never skip.
    import sys

    if sys.platform == "darwin":
        rc = monitor.run_monitor(
            ["install-unit", "--as", "smoke-peer",
             "--gateway", "http://100.64.189.91:8788",
             "--home", "/Users/smoke"])
        out = capsys.readouterr().out
        assert rc == 0
        assert plistlib.loads(out.encode())["Label"] == LABEL
    else:
        assert monitor._install_platform() == sys.platform


def test_linux_still_prints_the_systemd_unit(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "_install_platform", lambda: "linux")
    rc = monitor.run_monitor(
        ["install-unit", "--gateway", "http://100.64.189.91:8788",
         "--user", "celluser", "--home", "/home/celluser"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[Service]" in out and "SWARPH_SELF=%i" in out
