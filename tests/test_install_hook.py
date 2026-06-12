"""Tests for ``swarph install-hook`` (v0.7 PR-C)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from swarph_cli.commands.install_hook import (
    _HOOK_COMMAND,
    _has_swarph_hook,
    _hook_entry,
    _install,
    _settings_path,
    _uninstall,
    run_install_hook,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch) -> Iterator[Path]:
    """Isolate $HOME so `~/.claude/settings.json` writes land in tmp_path.

    Path.home() reads HOME on POSIX but USERPROFILE (then HOMEDRIVE+HOMEPATH)
    on Windows, so set all of them to keep the isolation effective cross-platform.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", tmp_path.drive)
    monkeypatch.setenv("HOMEPATH", str(tmp_path)[len(tmp_path.drive):])
    yield tmp_path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_settings_path_user_resolves_to_home_claude(isolated_home):
    assert _settings_path("user") == isolated_home / ".claude" / "settings.json"


def test_settings_path_project_resolves_to_cwd_claude(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert _settings_path("project") == tmp_path / ".claude" / "settings.json"


def test_settings_path_unknown_scope_raises():
    with pytest.raises(ValueError, match="unknown scope"):
        _settings_path("nope")


def test_hook_entry_shape_matches_claude_code_protocol():
    entry = _hook_entry()
    assert entry["matcher"] == ""
    hooks = entry["hooks"]
    assert len(hooks) == 1
    assert hooks[0]["type"] == "command"
    assert hooks[0]["command"] == _HOOK_COMMAND
    assert hooks[0]["timeout"] == 10


def test_has_swarph_hook_recognizes_own_command():
    entry = _hook_entry()
    assert _has_swarph_hook(entry) is True


def test_has_swarph_hook_rejects_foreign_command():
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
    assert _has_swarph_hook(foreign) is False


# ---------------------------------------------------------------------------
# _install — pure-dict transformations
# ---------------------------------------------------------------------------


def test_install_into_empty_settings_creates_full_block():
    after, changed = _install({})
    assert changed is True
    assert after["hooks"]["SessionStart"][0]["hooks"][0]["command"] == _HOOK_COMMAND


def test_install_idempotent_no_change_on_rerun():
    first, changed1 = _install({})
    second, changed2 = _install(first)
    assert changed1 is True
    assert changed2 is False
    assert first == second


def test_install_preserves_existing_unrelated_hooks():
    """Operator may have authored their own SessionStart entry; install
    must not trample it."""
    pre = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
            ]
        }
    }
    after, changed = _install(pre)
    assert changed is True
    entries = after["hooks"]["SessionStart"]
    assert len(entries) == 2  # foreign entry + new swarph entry
    foreign_present = any(
        any(h.get("command") == "echo other" for h in e["hooks"])
        for e in entries
    )
    assert foreign_present


def test_install_replaces_stale_swarph_entry():
    """If swarph entry exists with different timeout / shape, install
    updates in place (idempotency at intent-level, not byte-level)."""
    pre = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _HOOK_COMMAND,
                            "timeout": 5,  # different from canonical 10
                        }
                    ],
                }
            ]
        }
    }
    after, changed = _install(pre)
    assert changed is True
    entry = after["hooks"]["SessionStart"][0]
    assert entry["hooks"][0]["timeout"] == 10  # canonical value


def test_install_dedupes_multiple_swarph_entries():
    """If somehow two swarph entries exist (historical drift), keep the
    first + drop the rest."""
    swarph_entry = _hook_entry()
    pre = {"hooks": {"SessionStart": [swarph_entry, swarph_entry]}}
    after, changed = _install(pre)
    assert changed is True
    entries = after["hooks"]["SessionStart"]
    assert len(entries) == 1


def test_install_rejects_non_dict_hooks_block(capsys):
    pre = {"hooks": ["not-a-dict"]}
    with pytest.raises(SystemExit, match="not an object"):
        _install(pre)


def test_install_rejects_non_list_sessionstart(capsys):
    pre = {"hooks": {"SessionStart": "not-a-list"}}
    with pytest.raises(SystemExit, match="not an array"):
        _install(pre)


# ---------------------------------------------------------------------------
# _uninstall — preserve operator state
# ---------------------------------------------------------------------------


def test_uninstall_removes_swarph_entry_only():
    pre = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]},
                _hook_entry(),
            ]
        }
    }
    after, changed = _uninstall(pre)
    assert changed is True
    entries = after["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == "echo other"


def test_uninstall_no_op_when_swarph_entry_absent():
    pre = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}
            ]
        }
    }
    after, changed = _uninstall(pre)
    assert changed is False
    assert after == pre


def test_uninstall_cleans_up_empty_blocks():
    """Uninstall must leave no swarph residue: empty SessionStart array
    and empty hooks object both get pruned."""
    pre = {"hooks": {"SessionStart": [_hook_entry()]}}
    after, changed = _uninstall(pre)
    assert changed is True
    assert "hooks" not in after  # cleaned up


def test_uninstall_no_op_on_empty_settings():
    after, changed = _uninstall({})
    assert changed is False
    assert after == {}


# ---------------------------------------------------------------------------
# run_install_hook — CLI integration
# ---------------------------------------------------------------------------


def test_run_install_hook_writes_settings_file(isolated_home, capsys):
    rc = run_install_hook(argv=["--scope", "user"])
    assert rc == 0
    settings_file = isolated_home / ".claude" / "settings.json"
    assert settings_file.exists()
    parsed = json.loads(settings_file.read_text())
    assert parsed["hooks"]["SessionStart"][0]["hooks"][0]["command"] == _HOOK_COMMAND


def test_run_install_hook_dry_run_does_not_write(isolated_home, capsys):
    rc = run_install_hook(argv=["--scope", "user", "--dry-run"])
    assert rc == 0
    settings_file = isolated_home / ".claude" / "settings.json"
    assert not settings_file.exists()
    captured = capsys.readouterr()
    assert "dry-run" in captured.err
    assert "changed: True" in captured.err


def test_run_install_hook_idempotent_rerun(isolated_home, capsys):
    rc1 = run_install_hook(argv=["--scope", "user"])
    rc2 = run_install_hook(argv=["--scope", "user"])
    assert rc1 == 0 and rc2 == 0
    captured = capsys.readouterr()
    assert "no change needed" in captured.err  # second run reports no-op


def test_run_install_hook_uninstall_round_trip(isolated_home, capsys):
    settings_file = isolated_home / ".claude" / "settings.json"
    run_install_hook(argv=["--scope", "user"])
    assert settings_file.exists()
    rc = run_install_hook(argv=["--scope", "user", "--uninstall"])
    assert rc == 0
    parsed = json.loads(settings_file.read_text())
    assert "hooks" not in parsed  # full cleanup


def test_run_install_hook_refuses_corrupted_settings(isolated_home):
    settings_file = isolated_home / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("not valid json {")
    with pytest.raises(SystemExit, match="not valid JSON"):
        run_install_hook(argv=["--scope", "user"])


def test_run_install_hook_project_scope(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    rc = run_install_hook(argv=["--scope", "project"])
    assert rc == 0
    project_settings = tmp_path / ".claude" / "settings.json"
    assert project_settings.exists()


def test_run_install_hook_atomic_no_tempfile_residue(isolated_home):
    """Verifies _atomic_write_text is reused — no .tmp files left
    around the settings.json after a successful write."""
    run_install_hook(argv=["--scope", "user"])
    claude_dir = isolated_home / ".claude"
    contents = list(claude_dir.iterdir())
    assert contents == [claude_dir / "settings.json"]
