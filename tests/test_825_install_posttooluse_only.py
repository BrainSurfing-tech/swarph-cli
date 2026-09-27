"""Card #825 rework — hooks install must not put the prompt-side binding back.

Commander retired UserPromptSubmit, Stop, and StopFailure on 2026-09-26.
`swarph hooks add codegraph-on-grep` is what writes ~/.claude/settings.json.
On current main it writes all four events, so the next install undoes the retirement.
"""
import json

from swarph_cli.commands import hooks


def _events_for(settings: dict, command: str) -> set[tuple[str, str]]:
    found = set()
    for event, entries in (settings.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            matcher = entry.get("matcher", "")
            for action in entry.get("hooks") or []:
                if action.get("command") == command:
                    found.add((event, matcher))
    return found


def test_hooks_add_writes_only_the_posttooluse_codegraph_entry(tmp_path):
    """Fails on main: add writes UserPromptSubmit, Stop, and StopFailure too."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    home = tmp_path / "hooks"
    assert hooks.run_hooks(
        ["add", "codegraph-on-grep", "--yes"],
        settings_path=settings_path, hooks_home=home,
    ) == 0
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    command = str(home / "codegraph-on-grep.sh")
    assert _events_for(settings, command) == {("PostToolUse", "Bash")}


def test_hooks_add_drops_a_prompt_side_binding_already_in_settings(tmp_path):
    """A settings file that still has the retired events must not keep them."""
    home = tmp_path / "hooks"
    home.mkdir()
    command = str(home / "codegraph-on-grep.sh")
    other = str(home / "cell-resilience.sh")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [{"matcher": "", "hooks": [
                {"type": "command", "command": command}]}],
            "Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": command},
                {"type": "command", "command": other}]}],
            "StopFailure": [{"matcher": "", "hooks": [
                {"type": "command", "command": command}]}],
        }
    }), encoding="utf-8")
    assert hooks.run_hooks(
        ["add", "codegraph-on-grep", "--yes"],
        settings_path=settings_path, hooks_home=home,
    ) == 0
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert _events_for(settings, command) == {("PostToolUse", "Bash")}
    assert _events_for(settings, other) == {("Stop", "")}
