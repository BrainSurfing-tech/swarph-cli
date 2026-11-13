"""Executable substitutions in heredoc text must not escape identity routing."""
import json
import os
import shutil
import subprocess

import pytest

from swarph_cli import gh_identity
from swarph_cli.commands import gh_route


LIVE_COMMANDS = [
    pytest.param("cat <<EOF\n$(echo x\ngh pr merge 1)\nEOF", id="multiline-dollar"),
    pytest.param("cat <<EOF\n`echo x\ngh pr merge 1`\nEOF", id="multiline-backticks"),
    pytest.param("gh pr merge 1", id="bare-control"),
    pytest.param("cat <<EOF\n$(gh pr merge 1)\nEOF", id="single-line-control"),
    pytest.param("x=$(echo x\ngh pr merge 1)", id="outside-heredoc-control"),
]


@pytest.mark.parametrize("command", LIVE_COMMANDS)
def test_live_gh_substitution_is_detected(command):
    assert gh_identity.targets_gh(command) is True


@pytest.mark.parametrize("command", LIVE_COMMANDS)
def test_live_gh_is_routed_or_denied_by_the_real_hook(
        command, tmp_path, monkeypatch, capsys):
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"mapped-fixture": "fixture-login"}), encoding="utf-8")
    monkeypatch.setenv(gh_identity.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for peer in ("mapped-fixture", "unmapped-fixture"):
        monkeypatch.setenv("SWARPH_SELF", peer)
        assert gh_route.run_hook(json.dumps({
            "tool_name": "Bash", "tool_input": {"command": command},
        })) == 0
        output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        if peer == "mapped-fixture":
            assert output["updatedInput"]["command"] == (
                "export GH_TOKEN=$(gh auth token --user fixture-login); " + command)
        else:
            assert output["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", LIVE_COMMANDS)
@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
def test_bash_executes_the_fixture_gh_without_a_real_gh_binary(command, tmp_path):
    marker = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc"],
        input='gh() { printf "called\\n" >> "$GH_PROBE"; }\n' + command,
        text=True, capture_output=True, timeout=3, cwd=tmp_path,
        env={"PATH": os.defpath, "GH_PROBE": str(marker)},
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "called\n"


@pytest.mark.parametrize("command", [
    "cat <<'EOF'\n$(echo x\ngh pr merge 1)\nEOF",
    "cat <<'EOF'\n`echo x\ngh pr merge 1`\nEOF",
    "cat <<EOF\ngh pr merge 1\nEOF",
    "cat <<EOF\n\\$(echo x\ngh pr merge 1)\nEOF",
    "cat <<EOF\n\\`echo x\ngh pr merge 1\\`\nEOF",
    "cat <<EOF\n$(echo x\necho 'gh pr merge 1')\nEOF",
])
def test_heredoc_data_is_not_a_gh_invocation(command):
    assert gh_identity.targets_gh(command) is False
