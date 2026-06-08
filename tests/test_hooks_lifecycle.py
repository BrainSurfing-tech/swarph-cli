"""``swarph hooks`` lifecycle (T4) — published fail-closed + list + remove.

Exercises the T4 surface directly with tmp ``settings_path``/``hooks_home``:

* **published fails closed**: an ``@cell/hook`` reference is REFUSED before any
  resolution — non-zero exit, settings byte-for-byte unchanged, no script
  written. Driven via ``run_hooks(["add", "@x/y"], settings_path=, hooks_home=)``
  (the cleanest seam — ``run_hooks`` grew settings_path/hooks_home plumbing so
  the default-path constants don't need monkeypatching) AND via the factored
  ``_resolve_add_target`` helper which signals fail-closed by raising.
* **round-trip install → list → remove**: builtin installs, ``list_hooks``
  reports ``installed``, ``uninstall_hook`` strips both bindings + deletes the
  script, ``list_hooks`` then reports ``available``.
* **remove idempotent**: uninstall on empty settings is a no-op, returns 0.
* **remove preserves siblings**: unrelated top-level key + unrelated event
  survive an install+remove cycle.
* **list on empty**: builtin shows ``available``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.commands.hooks import (
    _load_settings,
    _resolve_add_target,
    install_hook,
    list_hooks,
    resolve_builtin,
    run_hooks,
    uninstall_hook,
)


# --------------------------------------------------------------------------- #
# published reference → fail closed
# --------------------------------------------------------------------------- #


def test_published_resolve_helper_fails_closed():
    with pytest.raises(ValueError) as exc:
        _resolve_add_target("@somecell/evilhook")
    msg = str(exc.value).lower()
    assert "not yet trusted" in msg or "not trusted" in msg
    # surfaces the safe alternative (builtin list) so the user has a next step
    assert "cell-resilience" in str(exc.value)


def test_published_add_mutates_nothing(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    pre = {
        "model": "opus",
        "hooks": {
            "PostToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": "other.sh"}]}
            ]
        },
    }
    raw = json.dumps(pre, indent=2)
    settings_path.write_text(raw, encoding="utf-8")
    before_bytes = settings_path.read_bytes()

    rc = run_hooks(
        ["add", "@somecell/evilhook"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc != 0
    # settings byte-for-byte unchanged, no script home created/populated
    assert settings_path.read_bytes() == before_bytes
    assert not hooks_home.exists() or not any(hooks_home.iterdir())


# --------------------------------------------------------------------------- #
# round-trip install → list → remove
# --------------------------------------------------------------------------- #


def test_install_list_remove_round_trip(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    bundle = resolve_builtin("cell-resilience")

    assert install_hook(
        bundle, settings_path=settings_path, hooks_home=hooks_home, assume_yes=True
    ) == 0

    script = (hooks_home / bundle.script_name).resolve()
    assert script.exists()

    # list → installed
    lines: list[str] = []
    assert list_hooks(settings_path=settings_path, hooks_home=hooks_home, out=lines.append) == 0
    joined = "\n".join(str(x) for x in lines)
    cr_line = [ln for ln in lines if "cell-resilience" in ln][0]
    assert "installed" in cr_line
    assert "available" not in cr_line

    # remove → both bindings stripped + script deleted
    assert uninstall_hook(
        bundle, settings_path=settings_path, hooks_home=hooks_home
    ) == 0

    settings = _load_settings(settings_path)
    hooks = settings.get("hooks", {})
    assert "StopFailure" not in hooks
    assert "Stop" not in hooks
    assert not script.exists()

    # list → available again
    lines2: list[str] = []
    list_hooks(settings_path=settings_path, hooks_home=hooks_home, out=lines2.append)
    cr_line2 = [ln for ln in lines2 if "cell-resilience" in ln][0]
    assert "available" in cr_line2
    assert "installed" not in cr_line2


def test_remove_idempotent_on_empty(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    bundle = resolve_builtin("cell-resilience")
    # nothing installed — must not raise, returns 0
    assert uninstall_hook(
        bundle, settings_path=settings_path, hooks_home=hooks_home
    ) == 0


def test_remove_preserves_siblings(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    settings_path.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "", "hooks": [{"type": "command", "command": "other.sh"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    bundle = resolve_builtin("cell-resilience")

    install_hook(bundle, settings_path=settings_path, hooks_home=hooks_home, assume_yes=True)
    uninstall_hook(bundle, settings_path=settings_path, hooks_home=hooks_home)

    settings = _load_settings(settings_path)
    assert settings["model"] == "opus"
    ptu = settings["hooks"]["PostToolUse"]
    assert ptu[0]["matcher"] == ""
    assert ptu[0]["hooks"][0]["command"] == "other.sh"


# --------------------------------------------------------------------------- #
# list on empty
# --------------------------------------------------------------------------- #


def test_list_on_empty_shows_available(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    lines: list[str] = []
    assert list_hooks(settings_path=settings_path, hooks_home=hooks_home, out=lines.append) == 0
    cr_line = [ln for ln in lines if "cell-resilience" in ln][0]
    assert "available" in cr_line
    assert "trust=builtin" in cr_line


def test_remove_cli_unknown_name_errors(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    rc = run_hooks(
        ["remove", "no-such-hook"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc != 0
