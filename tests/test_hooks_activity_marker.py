"""`swarph hooks touch-activity` + the `activity-marker` bundle (#2).

The watchdog's liveness = freshest mtime of {drain-cursor, activity-marker}. The marker
was touched only at turn-end (Stop hook), so a long autonomous turn (no turn-end) went
stale → false-stall → A1 wake INTO a working session. Fix: touch the marker on PreToolUse
(every tool call) too, via a bundled hook.

CRITICAL (spec §3): the hook must touch EXACTLY the path the watchdog reads, or it makes a
dead cell look alive — worse than no fix. So `touch-activity` reuses the watchdog's own
resolver; the agreement test pins that they can't drift.

Run: venv/bin/python -m pytest tests/test_hooks_activity_marker.py -v
"""
import os

import pytest

from swarph_cli.commands import hooks
from swarph_cli.commands.watchdog import _resolve_activity_marker_path


def _cwd_cell(tmp_path, monkeypatch, *, role="droplet", marker_override=None):
    # cwd must be a real, platform-valid dir — a hardcoded POSIX '/tmp' makes
    # load_cell reject the cell.yaml on Windows (then role silently falls back).
    body = f"name: {role}\nrole: {role}\ncwd: {tmp_path.as_posix()}\n"
    if marker_override:
        body += f"activity_marker_path: {marker_override}\n"
    (tmp_path / "cell.yaml").write_text(body)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))


# ── touch-activity resolves + touches the watchdog's exact path ────────────

def test_touch_creates_marker_at_watchdog_default_path(tmp_path, monkeypatch):
    _cwd_cell(tmp_path, monkeypatch, role="droplet")
    assert hooks.touch_activity([]) == 0
    expected = _resolve_activity_marker_path("droplet", None, None)
    assert expected.exists(), "the marker the watchdog would read must now exist"
    assert expected == tmp_path / "droplet-claude-active.txt"


def test_touch_honors_cell_yaml_marker_override(tmp_path, monkeypatch):
    override = (tmp_path / "sub" / "droplet-active.txt").as_posix()
    _cwd_cell(tmp_path, monkeypatch, role="droplet", marker_override=override)
    assert hooks.touch_activity([]) == 0
    from pathlib import Path
    assert Path(override).exists()


def test_touch_agrees_with_watchdog_resolver(tmp_path, monkeypatch):
    """Agreement by construction: the file touched == the resolver's path."""
    override = (tmp_path / "x" / "gpt-ops-active.txt").as_posix()
    _cwd_cell(tmp_path, monkeypatch, role="gpt-ops", marker_override=override)
    hooks.touch_activity([])
    watchdog_path = _resolve_activity_marker_path("gpt-ops", None, override)
    assert watchdog_path.exists(), "hook and watchdog must resolve the same file"


def test_touch_falls_back_to_swarph_cell_when_no_cell_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no cell.yaml
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("SWARPH_CELL", "gridiron")
    hooks.touch_activity([])
    assert (tmp_path / "gridiron-claude-active.txt").exists()


def test_touch_refreshes_mtime_on_existing_marker(tmp_path, monkeypatch):
    _cwd_cell(tmp_path, monkeypatch, role="droplet")
    marker = tmp_path / "droplet-claude-active.txt"
    marker.write_text("x")
    os.utime(marker, (1000, 1000))  # ancient
    hooks.touch_activity([])
    assert marker.stat().st_mtime > 1000, "an existing stale marker gets refreshed"


def test_touch_never_fails_even_on_unwritable_path(tmp_path, monkeypatch):
    """A liveness hook must NEVER fail a turn — always exit 0."""
    _cwd_cell(tmp_path, monkeypatch, role="droplet",
              marker_override="/proc/nonexistent/cannot/write.txt")
    assert hooks.touch_activity([]) == 0


# ── the bundle ─────────────────────────────────────────────────────────────

def test_activity_marker_bundle_binds_pretooluse_and_turn_end():
    b = hooks.resolve_builtin("activity-marker")
    events = {(bd.event, bd.matcher) for bd in b.bindings}
    assert ("PreToolUse", "") in events, "must fire on every tool call (the long-turn fix)"
    assert ("Stop", "") in events
    assert ("StopFailure", "") in events


def test_activity_marker_bundle_script_delegates_to_resolver():
    b = hooks.resolve_builtin("activity-marker")
    assert "hooks touch-activity" in b.script_body, \
        "script must delegate to the shared resolver (no path drift)"
    assert "exit 0" in b.script_body, "must never fail a turn"


def test_hooks_add_activity_marker_merges_bindings(tmp_path):
    settings = tmp_path / "settings.json"
    rc = hooks.run_hooks(
        ["add", "activity-marker", "--yes"],
        settings_path=settings,
        hooks_home=tmp_path / "hooks",
    )
    assert rc == 0
    import json
    data = json.loads(settings.read_text())
    assert "PreToolUse" in data["hooks"]
    assert "Stop" in data["hooks"]
