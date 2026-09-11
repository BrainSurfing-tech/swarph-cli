"""Card #830 — binding written at install time, handler resolved at run time.

Three load-bearing behaviours:
1. Unknown hook_event_name is LOUD (never silent Bash fallthrough).
2. Install stamps swarph-cli version into the generated script; list/status
   compares stamp vs running package.
3. install_hook refuses when the running handler does not advertise the
   events the bundle is about to bind.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli import __version__
from swarph_cli.commands import codegraph_hook as ch
from swarph_cli.commands import hooks


class _Stdin:
    def __init__(self, s: str):
        self._s = s

    def read(self):
        return self._s


def test_supported_hook_events_covers_bundle_bindings():
    advertised = ch.supported_hook_events()
    bundle = hooks.resolve_builtin("codegraph-on-grep")
    required = set(bundle.handler_events)
    assert required, "codegraph-on-grep must declare handler_events (#830)"
    assert required <= set(advertised)


def test_unknown_hook_event_is_loud_not_silent(monkeypatch, capsys):
    """>>> THE SHAPE THAT MADE BINDING-AHEAD-OF-CODE INVISIBLE. <<<"""
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "SessionStart",
        "session_id": "probe-830",
    })))
    assert ch.run_codegraph_hook([]) == 0
    out = capsys.readouterr().out
    assert out, "unknown event must emit, not silent exit 0"
    d = json.loads(out)
    ctx = d["hookSpecificOutput"]["additionalContext"]
    assert "unknown hook_event_name" in ctx
    assert "SessionStart" in ctx
    assert "#830" in ctx


def test_known_posttooluse_still_takes_bash_path(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": "ls -la"},
    })))
    assert ch.run_codegraph_hook([]) == 0
    assert capsys.readouterr().out == ""


def test_install_stamps_bundle_version(tmp_path, monkeypatch):
    if hooks.sys.platform == "win32":
        monkeypatch.setattr(hooks, "_find_windows_bash", lambda: Path("C:/Git/bash.exe"))
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    home = tmp_path / "hooks"
    bundle = hooks.resolve_builtin("codegraph-on-grep")
    assert "__SWARPH_CLI_VERSION__" in bundle.script_body
    lines: list[str] = []
    rc = hooks.install_hook(
        bundle, settings_path=settings, hooks_home=home,
        assume_yes=True, out=lines.append,
    )
    assert rc == 0
    script = (home / bundle.script_name).read_text(encoding="utf-8")
    assert f"swarph-cli-bundle-version: {__version__}" in script
    assert "__SWARPH_CLI_VERSION__" not in script


def test_list_reports_version_mismatch(tmp_path, monkeypatch):
    if hooks.sys.platform == "win32":
        monkeypatch.setattr(hooks, "_find_windows_bash", lambda: Path("C:/Git/bash.exe"))
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    home = tmp_path / "hooks"
    bundle = hooks.resolve_builtin("codegraph-on-grep")
    assert hooks.install_hook(
        bundle, settings_path=settings, hooks_home=home,
        assume_yes=True, out=lambda *_: None,
    ) == 0
    script_path = home / bundle.script_name
    script_path.write_text(
        script_path.read_text(encoding="utf-8").replace(
            f"swarph-cli-bundle-version: {__version__}",
            "swarph-cli-bundle-version: 0.0.0-stale",
        ),
        encoding="utf-8",
    )
    lines: list[str] = []
    assert hooks.list_hooks(
        settings_path=settings, hooks_home=home, out=lines.append,
    ) == 0
    blob = "\n".join(lines)
    assert "codegraph-on-grep" in blob and "[installed]" in blob
    assert "MISMATCH" in blob
    assert "0.0.0-stale" in blob


def test_install_refuses_when_handler_missing_events(tmp_path, monkeypatch):
    if hooks.sys.platform == "win32":
        monkeypatch.setattr(hooks, "_find_windows_bash", lambda: Path("C:/Git/bash.exe"))
    monkeypatch.setattr(
        "swarph_cli.commands.codegraph_hook.supported_hook_events",
        lambda: frozenset({"PostToolUse"}),  # missing UserPromptSubmit/Stop*
    )
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    home = tmp_path / "hooks"
    bundle = hooks.resolve_builtin("codegraph-on-grep")
    lines: list[str] = []
    rc = hooks.install_hook(
        bundle, settings_path=settings, hooks_home=home,
        assume_yes=True, out=lines.append,
    )
    assert rc == 1
    assert not (home / bundle.script_name).exists()
    blob = "\n".join(lines)
    assert "does not advertise" in blob
    assert "UserPromptSubmit" in blob
