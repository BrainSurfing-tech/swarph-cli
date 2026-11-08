"""Card #1499 — Cursor MCP file separate from Claude channel-serve (#975)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from swarph_cli.main import _VERB_HANDLERS

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MCP = ROOT / "plugins" / "swarph" / ".mcp.json"
CURSOR_MCP = ROOT / "plugins" / "swarph" / ".cursor-mcp.json"
CURSOR_PLUGIN = ROOT / "plugins" / "swarph" / ".cursor-plugin" / "plugin.json"

_HOST = re.compile(r"https?://[^\s\"']+|\b\d{1,3}(?:\.\d{1,3}){3}:\d+")


def test_claude_mcp_json_unchanged_channel_serve():
    spec = json.loads(CLAUDE_MCP.read_text(encoding="utf-8"))["mcpServers"]["swarph"]
    assert spec == {"command": "swarph", "args": ["channel-serve"]}
    assert _VERB_HANDLERS["channel-serve"].endswith("run_channel_serve")


def test_cursor_mcp_runs_mcp_server_without_shipped_env():
    text = CURSOR_MCP.read_text(encoding="utf-8")
    assert "env" not in text
    assert not _HOST.search(text), text
    spec = json.loads(text)["mcpServers"]["swarph"]
    assert spec["command"] == "swarph"
    assert spec["args"] == ["mcp-server"]
    assert _VERB_HANDLERS["mcp-server"].endswith("run_mcp_server")


def test_cursor_plugin_points_at_cursor_mcp_file():
    doc = json.loads(CURSOR_PLUGIN.read_text(encoding="utf-8"))
    assert doc["mcpServers"] == "./.cursor-mcp.json"
