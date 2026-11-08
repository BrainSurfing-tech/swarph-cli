"""Card #975 — the channel survives a tmux hop and a pipx install.

On current main the re-entry command carries only SWARPH_SPAWN, and the
plugin starts with system python3 -m swarph_cli.channel.
"""
import json
import sys
from pathlib import Path

from swarph_cli.commands import spawn
from swarph_cli.main import _VERB_HANDLERS

ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "plugins" / "swarph" / ".mcp.json"


def test_tmux_reentry_forwards_channel_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARPH_CHANNEL", raising=False)
    monkeypatch.delenv("SWARPH_CHANNEL_CELL", raising=False)
    monkeypatch.setenv("SWARPH_CHANNEL", "allowlisted")
    monkeypatch.setenv("SWARPH_CHANNEL_CELL", "gpu-wsl")
    monkeypatch.setattr(sys, "platform", "linux")
    cmd = spawn._tmux_session_command("tmux", "gpu-wsl", tmp_path)
    assert "SWARPH_CHANNEL=allowlisted" in cmd
    assert "SWARPH_CHANNEL_CELL=gpu-wsl" in cmd
    assert "SWARPH_SPAWN=1" in cmd


def test_psmux_reentry_forwards_channel_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARPH_CHANNEL", raising=False)
    monkeypatch.delenv("SWARPH_CHANNEL_CELL", raising=False)
    monkeypatch.setenv("SWARPH_CHANNEL", "dev")
    monkeypatch.setenv("SWARPH_CHANNEL_CELL", "cursor-win")
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = spawn._tmux_session_command("psmux", "cursor-win", tmp_path)
    script = cmd[-1]
    assert "$env:SWARPH_CHANNEL='dev'" in script
    assert "$env:SWARPH_CHANNEL_CELL='cursor-win'" in script
    assert "$env:SWARPH_SPAWN='1'" in script


def test_reentry_does_not_invent_a_channel_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARPH_CHANNEL", raising=False)
    monkeypatch.delenv("SWARPH_CHANNEL_CELL", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    cmd = spawn._tmux_session_command("tmux", "peer", tmp_path)
    assert not any(part.startswith("SWARPH_CHANNEL=") for part in cmd)


def test_plugin_mcp_uses_the_console_script_not_system_python():
    spec = json.loads(MCP.read_text(encoding="utf-8"))["mcpServers"]["swarph"]
    assert spec["command"] == "swarph"
    assert spec["args"] == ["channel-serve"]
    assert "python" not in spec["command"]
    assert "swarph_cli" not in " ".join(spec["args"])
    assert _VERB_HANDLERS["channel-serve"].endswith("run_channel_serve")
