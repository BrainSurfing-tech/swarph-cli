"""#462 — `swarph hooks verify`: prove an installed hook still CAN fire, per
hook, with absence as an explicit line and the swallow audit named.

The card's measurement: 2 of 7 live hooks on lab-ovh discard stderr, so a dead
hook and a quiet one produced the same observable. Existence is not firing —
but a hook whose interpreter or script is GONE cannot fire, and that half is
statically provable. These tests pin both halves of what the verb reports.
"""
from __future__ import annotations

import json

from swarph_cli.commands import hooks as hooks_mod
from swarph_cli.commands.hooks import verify_hooks


def _settings(tmp_path, hooks_block):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": hooks_block}), encoding="utf-8")
    return str(p)


def _hook(command, matcher=""):
    return [{"matcher": matcher, "hooks": [{"type": "command",
                                            "command": command}]}]


def test_all_resolvable_hooks_report_ok(tmp_path):
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sp = _settings(tmp_path, {"Stop": _hook(f"bash {script}")})
    lines: list[str] = []
    rc = verify_hooks(settings_path=sp, out=lines.append)
    assert rc == 0
    assert any("OK" in l and "Stop" in l for l in lines), lines


def test_missing_script_is_named_and_fails(tmp_path):
    sp = _settings(tmp_path, {"Stop": _hook(f"bash {tmp_path}/gone.sh")})
    lines: list[str] = []
    rc = verify_hooks(settings_path=sp, out=lines.append)
    assert rc == 1
    text = "\n".join(lines)
    assert "MISSING" in text and "gone.sh" in text


def test_unknown_verb_is_named_and_fails(tmp_path):
    sp = _settings(tmp_path, {"Stop": _hook("definitely-not-a-verb-462 --x")})
    lines: list[str] = []
    rc = verify_hooks(settings_path=sp, out=lines.append)
    assert rc == 1
    assert any("not found on PATH" in l for l in lines)


def test_swallowing_stderr_is_reported_but_not_broken(tmp_path):
    """The card's core finding: `2>/dev/null` makes death and quiet identical.
    The hook CAN still fire, so this is a loud warning, not a failure."""
    script = tmp_path / "h.py"
    script.write_text("pass\n", encoding="utf-8")
    sp = _settings(tmp_path,
                   {"PreToolUse": _hook(f"python3 {script} 2>/dev/null")})
    lines: list[str] = []
    rc = verify_hooks(settings_path=sp, out=lines.append)
    assert rc == 0, "a deaf hook is a warning, not a broken one"
    text = "\n".join(lines)
    assert "SWALLOWS-STDERR" in text and "2>/dev/null" in text


def test_redirection_order_controls_whether_stderr_is_discarded(tmp_path):
    script = tmp_path / "h.py"
    script.write_text("pass\n", encoding="utf-8")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _settings(first_dir, {"Stop": _hook(f"python3 {script} 2>&1 >/dev/null")})
    second = _settings(second_dir, {"Stop": _hook(f"python3 {script} >/dev/null 2>&1")})

    lines_first: list[str] = []
    lines_second: list[str] = []
    assert verify_hooks(settings_path=first, out=lines_first.append) == 0
    assert verify_hooks(settings_path=second, out=lines_second.append) == 0

    assert "SWALLOWS-STDERR" not in "\n".join(lines_first)
    assert "SWALLOWS-STDERR" in "\n".join(lines_second)


def test_exit_status_suppression_is_not_reported_as_stderr_swallow(tmp_path):
    script = tmp_path / "h.py"
    script.write_text("pass\n", encoding="utf-8")
    sp = _settings(tmp_path, {"Stop": _hook(f"python3 {script} || true")})
    lines: list[str] = []
    assert verify_hooks(settings_path=sp, out=lines.append) == 0
    text = "\n".join(lines)
    assert "SWALLOWS-STDERR" not in text
    assert "OK-COMPOUND" in text


def test_zero_hooks_is_an_explicit_line_not_an_empty_result(tmp_path):
    """#462's rule: absence must be SAID. An empty hook block and a missing
    one must never read the same."""
    sp = _settings(tmp_path, {})
    lines: list[str] = []
    rc = verify_hooks(settings_path=sp, out=lines.append)
    assert rc == 1
    assert any("ZERO hooks" in l for l in lines)


def test_missing_settings_file_says_so_in_words(tmp_path):
    lines: list[str] = []
    rc = verify_hooks(settings_path=str(tmp_path / "nope.json"),
                      out=lines.append)
    assert rc == 1
    assert any("does not exist" in l for l in lines)


def test_compound_command_is_ok_but_marked(tmp_path):
    """A compound shell line has more than one resolvable unit; the parts
    resolve, the firing stays unprovable — say so rather than claim OK."""
    script = tmp_path / "h.sh"
    script.write_text("exit 0\n", encoding="utf-8")
    sp = _settings(tmp_path, {"Stop": _hook(f"x=1; bash {script}")})
    lines: list[str] = []
    rc = verify_hooks(settings_path=sp, out=lines.append)
    assert rc == 0
    assert any("OK-COMPOUND" in l for l in lines)


def test_every_configured_hook_gets_a_line(tmp_path):
    """Per-hook reporting: 3 hooks in, 3 verdict lines out. A summary with no
    per-hook lines is exactly the 'empty result' shape the card forbids."""
    script = tmp_path / "h.sh"
    script.write_text("exit 0\n", encoding="utf-8")
    sp = _settings(tmp_path, {
        "Stop": _hook(f"bash {script}"),
        "PreToolUse": _hook(f"bash {script}", matcher="Bash"),
        "PostToolUse": _hook(f"bash {script}"),
    })
    lines: list[str] = []
    rc = verify_hooks(settings_path=sp, out=lines.append)
    assert rc == 0
    verdict_lines = [l for l in lines if l.startswith(("OK", "MISSING",
                                                       "SWALLOWS", "UNRESOLV"))]
    assert len(verdict_lines) == 3, lines
    assert any("3 hook(s)" in l for l in lines)


def test_non_command_hook_entries_are_named_not_skipped(tmp_path):
    sp = _settings(tmp_path, {"Stop": [{"matcher": "", "hooks":
                                        [{"type": "prompt", "prompt": "x"}]}]})
    lines: list[str] = []
    rc = verify_hooks(settings_path=sp, out=lines.append)
    assert rc == 1
    assert any("UNRESOLVABLE" in l and "prompt" in l for l in lines)


def test_the_verb_is_wired_into_the_cli(tmp_path):
    """Dispatch, not just the function: `run_hooks(['verify'])` reaches it."""
    script = tmp_path / "h.sh"
    script.write_text("exit 0\n", encoding="utf-8")
    sp = _settings(tmp_path, {"Stop": _hook(f"bash {script}")})
    rc = hooks_mod.run_hooks(["verify"], settings_path=sp)
    assert rc == 0
