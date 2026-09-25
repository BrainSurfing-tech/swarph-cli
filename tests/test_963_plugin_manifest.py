"""Card #963. Manifest fields Claude's marketplace schema requires."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(rel: str) -> dict:
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


def _package_version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("pyproject.toml has no version")


def test_marketplace_and_plugin_name_the_owner_and_version():
    market = _load(".claude-plugin/marketplace.json")
    plugin = _load("plugins/swarph/.claude-plugin/plugin.json")
    assert market["name"] == "swarph"
    assert market["owner"]["name"] == "BrainSurfing-tech"
    assert market["description"]
    assert plugin["name"] == "swarph"
    assert plugin["version"] == _package_version()
    assert plugin["description"]
    assert plugin["author"]["name"] == "BrainSurfing-tech"


def test_claude_plugin_validate_passes():
    claude = shutil.which("claude")
    if claude is None:
        pytest.skip("claude is not on PATH")
    proc = subprocess.run(
        [claude, "plugin", "validate", str(REPO)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
