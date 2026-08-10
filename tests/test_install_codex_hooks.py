import json
from pathlib import Path

import pytest

from swarph_cli.commands.install_codex_hooks import (
    _desired_hooks,
    _hooks_path,
    _install,
    _uninstall,
    run_install_codex_hooks,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", tmp_path.drive)
    monkeypatch.setenv("HOMEPATH", str(tmp_path)[len(tmp_path.drive):])
    return tmp_path


def test_desired_hooks_use_pinned_package_interpreter_not_path():
    desired = _desired_hooks()
    handlers = [handler for entries in desired.values() for entry in entries for handler in entry["hooks"]]
    assert {entry for entry in desired} == {"SessionStart", "PreToolUse", "Stop"}
    assert desired["SessionStart"][0]["matcher"] == ""
    assert all("-m swarph_cli" in handler["command"] for handler in handlers)
    assert all("commandWindows" in handler for handler in handlers)
    assert not any(handler["command"].startswith("swarph ") for handler in handlers)


def test_install_preserves_unrelated_handlers_and_is_idempotent():
    foreign = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo foreign"}]}]}}
    first, changed = _install(foreign)
    second, repeated = _install(first)
    assert changed is True
    assert repeated is False
    assert first == second
    assert first["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo foreign"


def test_uninstall_removes_only_swarph_handlers():
    foreign = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo foreign"}]}]}}
    installed, _ = _install(foreign)
    cleaned, changed = _uninstall(installed)
    assert changed is True
    assert cleaned == foreign


def test_install_preserves_an_unrelated_swarph_module_hook():
    foreign = {
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": '"python" -m swarph_cli other-command'}]}]
        }
    }
    installed, _ = _install(foreign)
    cleaned, _ = _uninstall(installed)
    assert cleaned == foreign


def test_user_install_writes_codex_hooks_file(isolated_home):
    assert _hooks_path("user") == isolated_home / ".codex" / "hooks.json"
    assert run_install_codex_hooks(["--scope", "user"]) == 0
    parsed = json.loads((isolated_home / ".codex" / "hooks.json").read_text())
    assert "SessionStart" in parsed["hooks"]
    assert "PreToolUse" in parsed["hooks"]
    assert "Stop" in parsed["hooks"]


def test_dry_run_does_not_write(isolated_home):
    assert run_install_codex_hooks(["--scope", "user", "--dry-run"]) == 0
    assert not (isolated_home / ".codex" / "hooks.json").exists()
