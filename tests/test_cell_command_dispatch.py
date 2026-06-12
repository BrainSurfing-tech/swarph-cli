# tests/test_cell_command_dispatch.py
import json
from pathlib import Path

import pytest

from swarph_cli.commands import cell as cell_cmd
from swarph_cli.capture import verify, harden


def test_unknown_subcommand_returns_2(capsys):
    assert cell_cmd.run_cell(["frobnicate"]) == 2


def test_no_args_prints_usage_returns_0(capsys):
    assert cell_cmd.run_cell([]) == 0
    assert "harden" in capsys.readouterr().err


def test_verify_returns_gate_code(monkeypatch, capsys):
    monkeypatch.setattr(
        verify, "verify_cell",
        lambda role: verify.VerifyResult(False, 4, "double-resume refused"),
    )
    rc = cell_cmd.run_cell(["verify", "droplet"])
    assert rc == 4
    assert "double-resume" in capsys.readouterr().err


def test_verify_ok_returns_0(monkeypatch):
    monkeypatch.setattr(
        verify, "verify_cell",
        lambda role: verify.VerifyResult(True, 0, "ok"),
    )
    assert cell_cmd.run_cell(["verify", "droplet"]) == 0


def test_harden_prints_artifacts_returns_0(monkeypatch, capsys):
    res = harden.HardenResult(
        role="droplet", launch_script="/x/launch-droplet.sh",
        manifest_path="/x/m.json", lineage_path="/x/l.jsonl",
        service="claude-tmux@droplet.service",
        enable_instructions=["# systemctl --user enable --now claude-tmux@droplet.service"],
    )
    monkeypatch.setattr(harden, "harden_cell", lambda role: res)
    rc = cell_cmd.run_cell(["harden", "droplet"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "launch-droplet.sh" in out
    assert "systemctl" in out


def test_main_routes_cell_verb(monkeypatch):
    # `swarph cell verify droplet` dispatches through main._VERB_HANDLERS
    from swarph_cli import main
    assert "cell" in main._VERB_HANDLERS
