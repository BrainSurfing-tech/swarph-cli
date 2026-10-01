"""Card #1013: `spawn` reads the channel mode from cell.yaml.

`channel: allowlisted | dev | off` on the cell, used when SWARPH_CHANNEL is
unset (env still overrides, incl. off). Muse cells take `ingress` for the
external-agent gate env. Anything invalid fails closed naming the field;
allowlisted without the marketplace on disk fails naming the file.

Run: python -m pytest tests/test_1013_spawn_channel.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from swarph_cli.cell import SCHEMA_VERSION_V1
from swarph_cli.commands.spawn import run_spawn


def _cell_yaml(tmp_path: Path, provider: str, channel=None) -> Path:
    payload = {
        "schema_version": SCHEMA_VERSION_V1,
        "name": "probe-cell",
        "role": "probe-cell",
        "cwd": str(tmp_path),
        "provider": provider,
    }
    if channel is not None:
        payload["channel"] = channel
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return p


def _registry(tmp_path: Path, monkeypatch, with_swarph: bool) -> Path:
    # The dev box may carry live gates; strip them so env_added reflects
    # only what the builders set.
    for var in ("MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS",
                "MUSE_EXPERIMENTAL_LOCAL_SESSION_MESSAGING"):
        monkeypatch.delenv(var, raising=False)
    # Session sidecars + config resolve under tmp, never the real home.
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    """Fake ~/.claude/plugins/known_marketplaces.json under an isolated HOME."""
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    registry = {"other": {"installLocation": str(tmp_path / "other")}}
    if with_swarph:
        market = tmp_path / "swarph-marketplace"
        market.mkdir()
        registry["swarph"] = {"installLocation": str(market)}
    (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
        json.dumps(registry), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _print(tmp_path: Path, cell_yaml: Path, capsys) -> tuple:
    """run_spawn --print-resolved on a temp cell.yaml.

    NOTE: plain `--print` is NOT used: argparse prefix-matching would route
    it to the existing `--print-id` flag and proceed to a REAL launch
    (observed hang). --print-resolved is exact and returns before resolve.
    """
    rc = run_spawn(["--cell", str(cell_yaml), "--print-resolved"])
    out, _ = capsys.readouterr()
    if rc != 0:
        return rc, None, None
    data = json.loads(out)
    return rc, data["argv"], data["env_added"]


def test_claude_allowlisted_from_field(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=True)
    p = _cell_yaml(tmp_path, "claude", channel="allowlisted")
    rc, argv, env_added = _print(tmp_path, p, capsys)
    assert rc == 0
    assert "--channels" in argv and "plugin:swarph@swarph" in argv
    assert env_added.get("SWARPH_CHANNEL_CELL") == "probe-cell"


def test_claude_dev_from_field(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=False)
    p = _cell_yaml(tmp_path, "claude", channel="dev")
    rc, argv, _ = _print(tmp_path, p, capsys)
    assert rc == 0
    assert "--dangerously-load-development-channels" in argv


def test_absent_channel_no_flag_no_ingress(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=False)
    p = _cell_yaml(tmp_path, "claude")
    rc, argv, env_added = _print(tmp_path, p, capsys)
    assert rc == 0
    assert not any(a.startswith("--channel") for a in argv)
    assert "SWARPH_CHANNEL_CELL" not in env_added
    assert "MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS" not in env_added


def test_env_off_overrides_field(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=True)
    monkeypatch.setenv("SWARPH_CHANNEL", "off")
    p = _cell_yaml(tmp_path, "claude", channel="allowlisted")
    rc, argv, env_added = _print(tmp_path, p, capsys)
    assert rc == 0
    assert "--channels" not in argv
    assert "SWARPH_CHANNEL_CELL" not in env_added


def test_env_allowlisted_without_field(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=True)
    monkeypatch.setenv("SWARPH_CHANNEL", "allowlisted")
    p = _cell_yaml(tmp_path, "claude")
    rc, argv, env_added = _print(tmp_path, p, capsys)
    assert rc == 0
    assert "--channels" in argv
    assert env_added.get("SWARPH_CHANNEL_CELL") == "probe-cell"


def test_muse_ingress_sets_env(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=False)
    p = _cell_yaml(tmp_path, "muse", channel="ingress")
    rc, argv, env_added = _print(tmp_path, p, capsys)
    assert rc == 0
    assert env_added.get("MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS") == "on"


def test_other_provider_channel_set_errors(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=True)
    p = _cell_yaml(tmp_path, "codex", channel="allowlisted")
    rc = run_spawn(["--cell", str(p), "--print-resolved"])
    _, err = capsys.readouterr()
    assert rc != 0
    assert "channel" in err


def test_claude_invalid_value_errors(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=True)
    p = _cell_yaml(tmp_path, "claude", channel="ingress")
    rc = run_spawn(["--cell", str(p), "--print-resolved"])
    _, err = capsys.readouterr()
    assert rc != 0
    assert "channel" in err


def test_muse_invalid_value_errors(tmp_path, monkeypatch, capsys):
    _registry(tmp_path, monkeypatch, with_swarph=False)
    p = _cell_yaml(tmp_path, "muse", channel="allowlisted")
    rc = run_spawn(["--cell", str(p), "--print-resolved"])
    _, err = capsys.readouterr()
    assert rc != 0
    assert "channel" in err


def test_bare_print_flag_is_rejected_not_launched(tmp_path, monkeypatch, capsys):
    """argparse prefix-matching must never route --print to --print-id."""
    _registry(tmp_path, monkeypatch, with_swarph=True)
    p = _cell_yaml(tmp_path, "claude", channel="allowlisted")
    rc = run_spawn(["--cell", str(p), "--print"])
    _, err = capsys.readouterr()
    assert rc == 2
    assert "ambiguous" in err


def test_allowlisted_missing_allowlist_errors(tmp_path, monkeypatch, capsys):
    home = _registry(tmp_path, monkeypatch, with_swarph=False)
    p = _cell_yaml(tmp_path, "claude", channel="allowlisted")
    rc = run_spawn(["--cell", str(p), "--print-resolved"])
    _, err = capsys.readouterr()
    assert rc != 0
    assert "known_marketplaces.json" in err or "marketplace" in err.lower()
    assert str(home) in err or "swarph" in err
