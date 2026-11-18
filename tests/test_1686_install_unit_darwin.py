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


def test_linux_still_prints_the_systemd_unit(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "_install_platform", lambda: "linux")
    rc = monitor.run_monitor(
        ["install-unit", "--gateway", "http://100.64.189.91:8788",
         "--user", "celluser", "--home", "/home/celluser"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[Service]" in out and "SWARPH_SELF=%i" in out
