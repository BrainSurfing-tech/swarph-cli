"""Card #1476 — GH_TOKEN counts as explicit only by position.

already_explicit is True ONLY when (i) GH_TOKEN= is an assignment prefix
of the gh simple command itself, or (ii) export GH_TOKEN= appears in the
same command list BEFORE the gh call. Heredoc bodies, backticks, echo
args and comments do not count; an export AFTER the gh call does not
count. Exercised through run_hook with stdin JSON on a mapped and an
unmapped cell: routed (not explicit) means rewrite for mapped and deny
for unmapped; explicit means the hook leaves the call unchanged.
"""
import json

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route

CASES = [
    pytest.param(
        "cat <<'EOF'\nGH_TOKEN=x\nEOF\ngh api user", False, id="a-heredoc"),
    pytest.param("GH_TOKEN=$(cmd) gh pr view", True, id="b-prefix"),
    pytest.param("export GH_TOKEN=x; gh pr view", True, id="c-export-before"),
    pytest.param("echo GH_TOKEN=x; gh pr view", False, id="d-echo-arg"),
    pytest.param("gh api x; export GH_TOKEN=y", False, id="e-export-after"),
]


@pytest.mark.parametrize("command,explicit", CASES)
@pytest.mark.parametrize("peer,mapped", [
    pytest.param("lab-ovh", True, id="mapped"),
    pytest.param("unmapped-fixture-cell", False, id="unmapped"),
])
def test_gh_token_explicit_only_by_position(
        tmp_path, monkeypatch, capsys, command, explicit, peer, mapped):
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"lab-ovh": "lab-ovh"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", peer)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    rc = gh_route.run_hook(json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    if explicit:
        assert out == ""
        return
    output = json.loads(out)["hookSpecificOutput"]
    if mapped:
        rewritten = output["updatedInput"]["command"]
        assert rewritten.startswith(
            "export GH_TOKEN=$(gh auth token --user lab-ovh); ")
        assert rewritten.endswith(command)
    else:
        assert output["permissionDecision"] == "deny"
