"""Tests for the ``swarph hooks`` settings.json merge core (T1).

PURE merge primitives only — no CLI yet. Covers:
  * ``_load_settings``: missing → {}, corrupt → ValueError (NEVER silent-{}),
    valid round-trip.
  * ``_save_settings``: atomic write, parent-dir auto-create, round-trip.
  * ``_merge_hook``: create, idempotent dedup, multi-command per matcher,
    multi-matcher per event, sibling preservation.
  * ``_unmerge_hook``: remove command, prune empty entry, prune empty event,
    no-op on absent, sibling preservation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.commands.hooks import (
    _load_settings,
    _save_settings,
    _merge_hook,
    _unmerge_hook,
)


# --------------------------------------------------------------------------
# _load_settings
# --------------------------------------------------------------------------

def test_load_settings_missing_returns_empty(tmp_path):
    missing = tmp_path / "nope" / "settings.json"
    assert _load_settings(missing) == {}


def test_load_settings_corrupt_raises_value_error(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json")
    with pytest.raises(ValueError) as excinfo:
        _load_settings(path)
    # path included in the message so the user can find the file to fix
    assert str(path) in str(excinfo.value)


def test_load_settings_valid_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    obj = {"model": "opus", "hooks": {"StopFailure": []}}
    path.write_text(json.dumps(obj))
    assert _load_settings(path) == obj


# --------------------------------------------------------------------------
# _save_settings
# --------------------------------------------------------------------------

def test_save_settings_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    obj = {"model": "opus", "hooks": {}}
    _save_settings(path, obj)
    assert _load_settings(path) == obj


def test_save_settings_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "settings.json"
    obj = {"hooks": {"PostToolUse": []}}
    _save_settings(nested, obj)
    assert nested.exists()
    assert _load_settings(nested) == obj


def test_save_settings_writes_complete_valid_json(tmp_path):
    path = tmp_path / "settings.json"
    obj = {"hooks": {"StopFailure": [{"matcher": "x", "hooks": []}]}}
    _save_settings(path, obj)
    # file content is complete + valid (atomicity: no partial write survives)
    text = path.read_text()
    assert json.loads(text) == obj
    # indent=2 formatting
    assert "\n  " in text


# --------------------------------------------------------------------------
# _merge_hook
# --------------------------------------------------------------------------

def test_merge_hook_creates_event_and_entry():
    settings = _merge_hook({}, "StopFailure", "rate_limit", "~/.swarph/hooks/x.sh")
    assert settings == {
        "hooks": {
            "StopFailure": [
                {
                    "matcher": "rate_limit",
                    "hooks": [{"type": "command", "command": "~/.swarph/hooks/x.sh"}],
                }
            ]
        }
    }


def test_merge_hook_is_idempotent():
    s = {}
    s = _merge_hook(s, "StopFailure", "rate_limit", "x.sh")
    s = _merge_hook(s, "StopFailure", "rate_limit", "x.sh")
    actions = s["hooks"]["StopFailure"][0]["hooks"]
    assert actions == [{"type": "command", "command": "x.sh"}]
    assert len(actions) == 1


def test_merge_hook_second_command_same_matcher_appends():
    s = {}
    s = _merge_hook(s, "StopFailure", "rate_limit", "x.sh")
    s = _merge_hook(s, "StopFailure", "rate_limit", "y.sh")
    entries = s["hooks"]["StopFailure"]
    assert len(entries) == 1
    actions = entries[0]["hooks"]
    assert len(actions) == 2
    assert {a["command"] for a in actions} == {"x.sh", "y.sh"}


def test_merge_hook_different_matcher_same_event_adds_entry():
    s = {}
    s = _merge_hook(s, "StopFailure", "rate_limit", "x.sh")
    s = _merge_hook(s, "StopFailure", "other", "y.sh")
    entries = s["hooks"]["StopFailure"]
    assert len(entries) == 2
    matchers = {e["matcher"] for e in entries}
    assert matchers == {"rate_limit", "other"}


def test_merge_hook_preserves_unrelated_keys_and_events():
    s = {
        "model": "opus",
        "hooks": {
            "PostToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": "pre.sh"}]}
            ]
        },
    }
    s = _merge_hook(s, "StopFailure", "rate_limit", "x.sh")
    # unrelated top-level key untouched
    assert s["model"] == "opus"
    # unrelated event untouched
    assert s["hooks"]["PostToolUse"] == [
        {"matcher": "", "hooks": [{"type": "command", "command": "pre.sh"}]}
    ]
    # new event added
    assert s["hooks"]["StopFailure"] == [
        {"matcher": "rate_limit", "hooks": [{"type": "command", "command": "x.sh"}]}
    ]


def test_merge_hook_empty_matcher_match_all():
    s = _merge_hook({}, "PostToolUse", "", "x.sh")
    entry = s["hooks"]["PostToolUse"][0]
    assert entry["matcher"] == ""
    # second merge with empty matcher dedups
    s = _merge_hook(s, "PostToolUse", "", "x.sh")
    assert len(s["hooks"]["PostToolUse"]) == 1
    assert len(s["hooks"]["PostToolUse"][0]["hooks"]) == 1


# --------------------------------------------------------------------------
# _unmerge_hook
# --------------------------------------------------------------------------

def test_unmerge_hook_removes_command_and_prunes_all():
    s = _merge_hook({}, "StopFailure", "rate_limit", "x.sh")
    s = _unmerge_hook(s, "StopFailure", "rate_limit", "x.sh")
    # entry pruned (hooks empty) → event pruned (list empty) → key gone
    assert s["hooks"] == {}


def test_unmerge_hook_noop_on_absent_command():
    s = _merge_hook({}, "StopFailure", "rate_limit", "x.sh")
    before = json.loads(json.dumps(s))
    s = _unmerge_hook(s, "StopFailure", "rate_limit", "not-there.sh")
    assert s == before  # unchanged, no raise


def test_unmerge_hook_noop_on_absent_event():
    s = {"model": "opus"}
    s = _unmerge_hook(s, "StopFailure", "rate_limit", "x.sh")
    assert s == {"model": "opus"}  # unchanged, no raise


def test_unmerge_hook_noop_on_absent_hooks_key():
    s = _unmerge_hook({}, "StopFailure", "rate_limit", "x.sh")
    assert s == {}  # no raise, unchanged


def test_unmerge_hook_preserves_sibling_command():
    s = {}
    s = _merge_hook(s, "StopFailure", "rate_limit", "x.sh")
    s = _merge_hook(s, "StopFailure", "rate_limit", "y.sh")
    s = _unmerge_hook(s, "StopFailure", "rate_limit", "x.sh")
    # entry + sibling command intact
    entries = s["hooks"]["StopFailure"]
    assert len(entries) == 1
    actions = entries[0]["hooks"]
    assert actions == [{"type": "command", "command": "y.sh"}]


def test_unmerge_hook_preserves_sibling_matcher_and_event():
    s = {"model": "opus"}
    s = _merge_hook(s, "StopFailure", "rate_limit", "x.sh")
    s = _merge_hook(s, "StopFailure", "other", "y.sh")
    s = _merge_hook(s, "PostToolUse", "", "z.sh")
    s = _unmerge_hook(s, "StopFailure", "rate_limit", "x.sh")
    # rate_limit entry pruned, but 'other' entry + PostToolUse event + model intact
    assert s["model"] == "opus"
    sf = s["hooks"]["StopFailure"]
    assert len(sf) == 1
    assert sf[0]["matcher"] == "other"
    assert s["hooks"]["PostToolUse"] == [
        {"matcher": "", "hooks": [{"type": "command", "command": "z.sh"}]}
    ]
