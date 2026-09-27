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


def _codegraph_command(home):
    """The command string install writes. On Windows that is bash plus the path."""
    bundle = hooks.resolve_builtin("codegraph-on-grep")
    return hooks._installed_command(bundle, home)


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
    assert _events_for(settings, _codegraph_command(home)) == {("PostToolUse", "Bash")}


def test_hooks_add_drops_a_prompt_side_binding_already_in_settings(tmp_path):
    """A settings file that still has the retired events must not keep them."""
    home = tmp_path / "hooks"
    home.mkdir()
    command = _codegraph_command(home)
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


def _add(settings_path, home, body: dict) -> int:
    settings_path.write_text(json.dumps(body), encoding="utf-8")
    return hooks.run_hooks(
        ["add", "codegraph-on-grep", "--yes"],
        settings_path=settings_path, hooks_home=home,
    )


def test_sessionstart_group_with_empty_hooks_is_kept(tmp_path):
    """An empty hooks list never held the command. The sweep must not delete it."""
    home = tmp_path / "hooks"
    settings_path = tmp_path / "settings.json"
    group = {"matcher": "startup", "hooks": []}
    assert _add(settings_path, home, {"hooks": {"SessionStart": [group]}}) == 0
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooks"]["SessionStart"] == [group]


def test_matcher_only_notification_group_is_kept(tmp_path):
    """A group with no hooks key never held the command. Leave it byte-for-byte."""
    home = tmp_path / "hooks"
    settings_path = tmp_path / "settings.json"
    group = {"matcher": ""}
    assert _add(settings_path, home, {"hooks": {"Notification": [group]}}) == 0
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooks"]["Notification"] == [group]


def test_non_dict_entry_in_an_unrelated_event_does_not_crash(tmp_path):
    """A string in another event is not a group. Install exits 0 and leaves it."""
    home = tmp_path / "hooks"
    settings_path = tmp_path / "settings.json"
    assert _add(settings_path, home, {"hooks": {"Notification": ["junk"]}}) == 0
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooks"]["Notification"] == ["junk"]
