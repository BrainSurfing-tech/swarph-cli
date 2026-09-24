"""instable-opencode-plugin — the OpenCode plugin that wires swarph hooks.

opencode's only hook surface is a JS plugin auto-loaded from the config dir's
plugins/ tree. This ships the plugin payload, the installer verb, and the
cell-side write. These tests pin the CONTRACT, not live opencode execution (no
opencode binary in CI — the same posture as the codex/cursor tests).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from swarph_cli.commands import install_opencode_plugin as plugin
from swarph_cli.commands.install_opencode_plugin import (
    _PLUGIN_NAME,
    _render,
    _render_plugin,
    cell_plugin_path,
    ensure_cell_plugin,
    run_install_opencode_plugin,
)
from swarph_cli.commands.wake_hook_output import _ARM_HARNESSES, _arm_instruction


def _py_literal(text):
    """The exact path opencode would decode from `const PY = <literal>;`."""
    m = re.search(r'const PY = ("(?:[^"\\]|\\.)*");', text)
    assert m, "const PY = <literal>; not found in the rendered plugin"
    return json.loads(m.group(1))


def test_render_pins_the_interpreter_and_drops_the_placeholder():
    """The plugin must shell out to the SAME Python that owns swarph, not a bare
    `python` that PATH may resolve stalely (the codex installer's rule)."""
    rendered = _render()
    assert "@PYTHON@" not in rendered
    assert Path(sys.executable).name in rendered


def test_render_is_a_VALID_JS_string_for_spaces_and_backslashes():
    """>>> shlex.quote WAS the shipped defect (#423 review). <<< ``@PYTHON@`` sits
    inside ``const PY = "@PYTHON@";`` — a JS string literal, not shell text. The
    embedded value must be a valid double-quoted, backslash-escaped JS string that
    decodes back to the EXACT path, including Windows ``C:\\Program Files`` (whose
    backslashes shlex.quote left raw, becoming invalid JS escapes)."""
    for path in (
        "/usr/bin/python3",
        "/home/a b/python",
        r"C:\Program Files\Python\python.exe",
    ):
        assert _py_literal(_render_plugin(path)) == path, path


def test_plugin_payload_is_the_hook_factory():
    """The shipped module exports the named + default hook factory, which is what
    opencode picks up from the plugins dir (measured: the operator's own plugin
    loads with a config containing only $schema)."""
    from importlib import resources

    text = resources.files("swarph_cli.payloads.opencode").joinpath("plugin.js").read_text(
        encoding="utf-8"
    )
    assert "export const SwarphOpencodePlugin" in text
    assert "tool.execute.before" in text
    assert "experimental.session.compacting" in text


def test_install_user_scope_writes_into_the_global_plugins_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    run_install_opencode_plugin(["--scope", "user"])
    target = tmp_path / ".config" / "opencode" / "plugins" / _PLUGIN_NAME
    assert target.is_file()
    assert "@PYTHON@" not in target.read_text(encoding="utf-8")


def test_install_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    run_install_opencode_plugin(["--scope", "user"])
    target = tmp_path / ".config" / "opencode" / "plugins" / _PLUGIN_NAME
    first = target.read_text(encoding="utf-8")
    run_install_opencode_plugin(["--scope", "user"])
    assert target.read_text(encoding="utf-8") == first


def test_uninstall_removes_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    run_install_opencode_plugin(["--scope", "user"])
    target = tmp_path / ".config" / "opencode" / "plugins" / _PLUGIN_NAME
    assert target.is_file()
    run_install_opencode_plugin(["--scope", "user", "--uninstall"])
    assert not target.exists()


def test_project_scope_writes_into_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_install_opencode_plugin(["--scope", "project"])
    target = tmp_path / ".opencode" / "plugins" / _PLUGIN_NAME
    assert target.is_file()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_install_opencode_plugin(["--scope", "project", "--dry-run"])
    assert not (tmp_path / ".opencode" / "plugins" / _PLUGIN_NAME).exists()


def test_cell_plugin_path_lives_in_the_isolated_config_dir(tmp_path):
    """The cell's config is XDG-relocated to <cwd>/.opencode-cell/config, so the
    plugin must land at <cwd>/.opencode-cell/config/opencode/plugins/ — NOT under
    ~/.config/opencode, which the cell never reads."""
    assert cell_plugin_path(tmp_path) == (
        tmp_path / ".opencode-cell" / "config" / "opencode" / "plugins" / _PLUGIN_NAME
    )


def test_ensure_cell_plugin_writes_and_is_idempotent(tmp_path):
    ensure_cell_plugin(tmp_path)
    target = cell_plugin_path(tmp_path)
    assert target.is_file()
    first = target.read_text(encoding="utf-8")
    assert "@PYTHON@" not in first
    ensure_cell_plugin(tmp_path)
    assert target.read_text(encoding="utf-8") == first


def test_ensure_cell_plugin_never_raises_even_if_unwritable(tmp_path, monkeypatch):
    """A failed plugin write must not block the spawn — worst case the cell runs
    unhooked (the failure-mode invariant of every hook callback)."""
    def boom(*a, **k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(plugin, "_render", boom)
    ensure_cell_plugin(tmp_path)  # must not raise


def test_opencode_is_an_ARM_harness_not_a_verify_harness():
    """The wake lives in the harness for opencode (arm-instruction emitted as the
    tail -F inbox.log | dm_notify_filter watch), not in a swarph monitor push
    sink the way cursor is."""
    assert "opencode" in _ARM_HARNESSES
    instruction = _arm_instruction("opencode-1", "install-time --cell")
    assert "tail" in instruction and "inbox.log" in instruction
