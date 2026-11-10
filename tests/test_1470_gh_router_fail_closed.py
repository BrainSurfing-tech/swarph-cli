"""The hook must not leave a gh invocation on the ambient identity."""
import json

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route


@pytest.mark.parametrize("command", [
    "# Check what's open\ngh pr list",
    '# Check what"s open\ngh pr list',
    "echo 'unterminated\ngh pr list",
    "# what's open; export GH_TOKEN=x\ngh pr list",
    'echo "; export GH_TOKEN=x"; gh pr list',
])
def test_quoted_text_cannot_hide_an_unrouted_gh(tmp_path, monkeypatch, capsys, command):
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "unmapped-fixture-cell")
    rc = gh_route.run_hook(json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }))
    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_deep_command_substitution_denies_instead_of_crashing(capsys):
    rc = gh_route.run_hook(json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": '"$(' * 3000 + "gh pr list"},
    }))
    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("payload", ["{not json", "[]", '{"tool_input":"invalid"}',
                                   '{"tool_input":{"command":42}}'])
def test_hook_errors_deny_instead_of_leaving_the_call_unchanged(payload, capsys):
    assert gh_route.run_hook(payload) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", [
    "cat <<EOF\nPR #679 head: $(gh pr view 679 --json headRefOid -q .headRefOid)\nEOF",
    "cat <<EOF\n# $(gh api user -q .login)\nEOF",
    r"echo a\ #$(gh pr list)",
    r"echo a\\#$(gh pr list)",
    "echo ${X:- #$(gh pr list)}",
], ids=["heredoc-header", "heredoc-hash", "escaped-space", "two-backslashes", "parameter-word"])
@pytest.mark.parametrize("peer", ["lab-ovh", "unmapped-fixture-cell"], ids=["mapped", "unmapped"])
def test_hash_context_does_not_hide_command_substitution(tmp_path, monkeypatch, capsys, command, peer):
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"lab-ovh": "lab-ovh"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", peer)
    assert gh_route.run_hook(json.dumps({"tool_name":"Bash", "tool_input":{"command":command}})) == 0
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    if peer == "lab-ovh":
        rewritten = output["updatedInput"]["command"]
        assert rewritten.startswith("export GH_TOKEN=$(gh auth token --user lab-ovh); ")
        assert rewritten.endswith(command)
    else:
        assert output["permissionDecision"] == "deny"
