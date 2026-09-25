"""Card #964. The channel serves only the cell spawn named."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from swarph_cli.commands.spawn import _spawn_env_base


def _run(root: Path, self_name: str, channel_cell: str | None) -> subprocess.CompletedProcess[str]:
    side = root / self_name / "mesh-sidecar"
    side.mkdir(parents=True)
    (side / "inbox.log").write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["SWARPH_SELF"] = self_name
    env["SWARPH_STATE"] = str(root)
    env["SWARPH_CHANNEL"] = "allowlisted"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    if channel_cell is None:
        env.pop("SWARPH_CHANNEL_CELL", None)
    else:
        env["SWARPH_CHANNEL_CELL"] = channel_cell
    return subprocess.run(
        [sys.executable, "-m", "swarph_cli.channel"],
        input=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }) + "\n",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _untouched(root: Path, self_name: str) -> None:
    side = root / self_name / "mesh-sidecar"
    assert not (side / "channel_cursor.json").exists()
    assert not (side / "channel_heartbeat.json").exists()
    assert not (side / "channel.lock").exists()


def test_mismatch_refuses(tmp_path):
    proc = _run(tmp_path, "lab-ovh", "science-claude")
    assert "claude/channel" not in proc.stdout
    assert "lab-ovh" in proc.stderr and "science-claude" in proc.stderr
    _untouched(tmp_path, "lab-ovh")


def test_missing_channel_cell_refuses(tmp_path):
    proc = _run(tmp_path, "cursor-lin", None)
    assert "claude/channel" not in proc.stdout
    assert "cursor-lin" in proc.stderr
    _untouched(tmp_path, "cursor-lin")


def test_matching_cell_still_serves(tmp_path):
    proc = _run(tmp_path, "cursor-lin", "cursor-lin")
    assert "claude/channel" in proc.stdout
    side = tmp_path / "cursor-lin" / "mesh-sidecar"
    assert (side / "channel_cursor.json").exists()


def test_dry_run_prints_the_channel_stamp_from_the_launch_env(monkeypatch, capsys):
    from swarph_cli.commands.spawn import _print_dry_run

    class Cell:
        name = "science-claude"
        role = "science-claude"
        source_path = "cell.yaml"
        schema_version = 1
        cwd = "/tmp"
        provider = "claude"
        lineage = None
        starter_prompt_path = None

    monkeypatch.delenv("SWARPH_CHANNEL", raising=False)
    _print_dry_run(Cell(), "sid", False, ["claude"])
    assert "SWARPH_CHANNEL_CELL" not in capsys.readouterr().err
    for mode in ("allowlisted", "dev"):
        monkeypatch.setenv("SWARPH_CHANNEL", mode)
        _print_dry_run(Cell(), "sid", False, ["claude"])
        err = capsys.readouterr().err
        assert "#   channel:     plugin:swarph@swarph, SWARPH_CHANNEL_CELL=science-claude" in err


def test_spawn_sets_channel_cell_only_when_channel_is_on(monkeypatch):
    class Cell:
        name = "science-claude"

    monkeypatch.delenv("SWARPH_CHANNEL", raising=False)
    assert "SWARPH_CHANNEL_CELL" not in _spawn_env_base(Cell())
    monkeypatch.setenv("SWARPH_CHANNEL", "allowlisted")
    assert _spawn_env_base(Cell())["SWARPH_CHANNEL_CELL"] == "science-claude"
    monkeypatch.setenv("SWARPH_CHANNEL", "dev")
    assert _spawn_env_base(Cell())["SWARPH_CHANNEL_CELL"] == "science-claude"
