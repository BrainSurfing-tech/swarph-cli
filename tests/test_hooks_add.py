"""``swarph hooks add`` (T3) — builtin + local install, show-before-write.

Exercises ``install_hook`` / ``resolve_local`` directly with tmp paths
(no CLI shell-out). Covers:

* add builtin (writes executable script + merges both bindings into settings)
* idempotent re-install (no duplicate actions per matcher)
* preserves pre-existing settings (other top-level keys + other events)
* resolve_local (manifest + script body → HookBundle) + missing-manifest error
* local requires confirm (writes NOTHING on "n"; installs with assume_yes)
* activation note printed via the injected ``out`` sink
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from swarph_cli.commands.hooks import (
    HookBundle,
    _load_settings,
    install_hook,
    resolve_builtin,
    resolve_local,
)


# --------------------------------------------------------------------------- #
# add builtin
# --------------------------------------------------------------------------- #


def test_add_builtin_writes_script_and_merges_bindings(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = install_hook(
        resolve_builtin("cell-resilience"),
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=True,
    )
    assert rc == 0

    script = hooks_home / "cell-resilience.sh"
    assert script.exists()
    # executable bit set
    if sys.platform != "win32":  # POSIX file-mode bits not representable on Windows
        assert os.stat(script).st_mode & 0o111

    expected_command = str(script.resolve())

    settings = _load_settings(settings_path)
    hooks = settings["hooks"]

    sf = hooks["StopFailure"]
    assert len(sf) == 1
    assert sf[0]["matcher"] == "rate_limit"
    assert sf[0]["hooks"][0]["command"] == expected_command

    st = hooks["Stop"]
    assert len(st) == 1
    assert st[0]["matcher"] == ""
    assert st[0]["hooks"][0]["command"] == expected_command


def test_add_builtin_is_idempotent(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    bundle = resolve_builtin("cell-resilience")

    assert install_hook(bundle, settings_path=settings_path, hooks_home=hooks_home, assume_yes=True) == 0
    assert install_hook(bundle, settings_path=settings_path, hooks_home=hooks_home, assume_yes=True) == 0

    settings = _load_settings(settings_path)
    hooks = settings["hooks"]
    # one entry per matcher, one action per entry — no duplicates
    assert len(hooks["StopFailure"]) == 1
    assert len(hooks["StopFailure"][0]["hooks"]) == 1
    assert len(hooks["Stop"]) == 1
    assert len(hooks["Stop"][0]["hooks"]) == 1


def test_add_builtin_preserves_existing_settings(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    settings_path.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "/bin/true"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    rc = install_hook(
        resolve_builtin("cell-resilience"),
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=True,
    )
    assert rc == 0

    settings = _load_settings(settings_path)
    assert settings["model"] == "opus"
    # pre-existing PostToolUse untouched
    ptu = settings["hooks"]["PostToolUse"]
    assert ptu[0]["matcher"] == "Bash"
    assert ptu[0]["hooks"][0]["command"] == "/bin/true"
    # new bindings present alongside
    assert "StopFailure" in settings["hooks"]
    assert "Stop" in settings["hooks"]


# --------------------------------------------------------------------------- #
# all-or-nothing: load/merge failure leaves nothing written
# --------------------------------------------------------------------------- #


def test_install_aborts_on_non_object_settings_writes_no_script(tmp_path):
    # A valid-but-non-object settings.json (a truncated/fragment file) must
    # abort the install BEFORE the script is written — the all-or-nothing
    # guarantee. install_hook lets the ValueError propagate; the CLI layer
    # catches it and returns non-zero.
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("[]", encoding="utf-8")  # valid JSON, not an object
    hooks_home = tmp_path / "hooks"

    with pytest.raises(ValueError):
        install_hook(
            resolve_builtin("cell-resilience"),
            settings_path=settings_path,
            hooks_home=hooks_home,
            assume_yes=True,
        )

    # NOTHING written: no script orphaned in hooks_home (dir absent or empty),
    # and the user's settings.json is untouched.
    assert not (hooks_home / "cell-resilience.sh").exists()
    if hooks_home.exists():
        assert list(hooks_home.iterdir()) == []
    assert settings_path.read_text(encoding="utf-8") == "[]"


def test_install_aborts_on_corrupt_settings_writes_no_script(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{ not json", encoding="utf-8")
    hooks_home = tmp_path / "hooks"

    with pytest.raises(ValueError):
        install_hook(
            resolve_builtin("cell-resilience"),
            settings_path=settings_path,
            hooks_home=hooks_home,
            assume_yes=True,
        )

    assert not (hooks_home / "cell-resilience.sh").exists()
    if hooks_home.exists():
        assert list(hooks_home.iterdir()) == []


# --------------------------------------------------------------------------- #
# resolve_local
# --------------------------------------------------------------------------- #


def _make_local_bundle_dir(tmp_path) -> Path:
    d = tmp_path / "mybundle"
    d.mkdir()
    (d / "hook.json").write_text(
        json.dumps(
            {
                "name": "myhook",
                "description": "a local hook",
                "script_name": "myhook.sh",
                "bindings": [
                    {"event": "PostToolUse", "matcher": "Bash"},
                    {"event": "Stop", "matcher": ""},
                ],
            }
        ),
        encoding="utf-8",
    )
    (d / "myhook.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    return d


def test_resolve_local_reads_manifest_and_body(tmp_path):
    d = _make_local_bundle_dir(tmp_path)
    bundle = resolve_local(d)

    assert isinstance(bundle, HookBundle)
    assert bundle.name == "myhook"
    assert bundle.trust == "local"
    assert bundle.script_name == "myhook.sh"
    assert bundle.script_body == "#!/bin/sh\necho hi\n"
    events = {(b.event, b.matcher) for b in bundle.bindings}
    assert events == {("PostToolUse", "Bash"), ("Stop", "")}


def test_resolve_local_missing_manifest_errors(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises((FileNotFoundError, ValueError)):
        resolve_local(d)


def test_resolve_local_missing_script_errors(tmp_path):
    d = tmp_path / "noscript"
    d.mkdir()
    (d / "hook.json").write_text(
        json.dumps(
            {
                "name": "x",
                "description": "d",
                "script_name": "absent.sh",
                "bindings": [{"event": "Stop", "matcher": ""}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises((FileNotFoundError, ValueError)):
        resolve_local(d)


# --------------------------------------------------------------------------- #
# local requires confirmation
# --------------------------------------------------------------------------- #


def test_local_aborts_on_no_and_writes_nothing(tmp_path, monkeypatch):
    d = _make_local_bundle_dir(tmp_path)
    bundle = resolve_local(d)

    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    rc = install_hook(
        bundle,
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=False,
    )
    assert rc != 0
    # wrote NOTHING — no settings, no copied script, no hooks dir contents
    assert not settings_path.exists()
    assert not (hooks_home / "myhook.sh").exists()


def test_local_installs_with_assume_yes(tmp_path):
    d = _make_local_bundle_dir(tmp_path)
    bundle = resolve_local(d)

    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = install_hook(
        bundle,
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=True,
    )
    assert rc == 0
    script = hooks_home / "myhook.sh"
    assert script.exists()
    if sys.platform != "win32":  # POSIX file-mode bits not representable on Windows
        assert os.stat(script).st_mode & 0o111

    settings = _load_settings(settings_path)
    assert "PostToolUse" in settings["hooks"]
    assert "Stop" in settings["hooks"]


# --------------------------------------------------------------------------- #
# show-before-write + activation note
# --------------------------------------------------------------------------- #


def test_activation_note_printed(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    lines: list[str] = []

    rc = install_hook(
        resolve_builtin("cell-resilience"),
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=True,
        out=lines.append,
    )
    assert rc == 0
    joined = "\n".join(str(x) for x in lines)
    assert "/hooks" in joined


def test_summary_shows_bindings(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    lines: list[str] = []

    install_hook(
        resolve_builtin("cell-resilience"),
        settings_path=settings_path,
        hooks_home=hooks_home,
        assume_yes=True,
        out=lines.append,
    )
    joined = "\n".join(str(x) for x in lines)
    # human-readable summary names the events
    assert "StopFailure" in joined
    assert "Stop" in joined
