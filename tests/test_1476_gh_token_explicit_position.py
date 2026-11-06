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
    # #4423 shapes: each must stay ROUTED (explicit False) on both cells.
    pytest.param(
        "cat <<'EOF'\nit's\nEOF\ncat <<'B'\nexport GH_TOKEN=x\nB\ngh pr view",
        False, id="s1-heredoc-quote-then-heredoc-export"),
    pytest.param(
        "cat <<'EOF'\nit's\nEOF\necho 'x; export GH_TOKEN=y; '; gh pr view",
        False, id="s2-heredoc-quote-then-quoted-export"),
    pytest.param(
        "cat <<'EOF'\nEOF \nexport GH_TOKEN=x\nEOF\ngh pr view",
        False, id="s3a-terminator-trailing-space"),
    pytest.param(
        "cat <<'EOF'\n EOF\nexport GH_TOKEN=x\nEOF\ngh pr view",
        False, id="s3b-terminator-leading-space"),
    pytest.param(
        "echo $'a\\'; export GH_TOKEN=x; \\''; gh pr view",
        False, id="s4-ansi-c-escaped-quote"),
    pytest.param(
        "export -n GH_TOKEN=x; gh pr view", False, id="s5-export-option"),
    pytest.param(
        "export GH_TOKEN=x | gh pr view", False, id="s6a-export-piped"),
    pytest.param(
        "export GH_TOKEN=x & gh pr view", False, id="s6b-export-backgrounded"),
    # #4426 shapes: heredoc inside $( inside double quotes. The heredoc
    # body is raw text: its apostrophes and ) never close the quote or
    # the substitution, and an export in the body is not explicit.
    pytest.param(
        "BODY=\"$(cat <<'EOF'\nIt's\n1) item\nexport GH_TOKEN=x\nEOF\n)\""
        "; gh pr create --body \"$BODY\"",
        False, id="s7-body-heredoc-paren-apostrophe"),
    pytest.param(
        "BODY=\"$(cat <<'EOF'\nIt's\nexport GH_TOKEN=x\nEOF\n)\""
        "; gh pr create --body \"$BODY\"",
        False, id="s7b-body-heredoc-apostrophe"),
    pytest.param(
        "BODY=\"$(cat <<'EOF'\nIts\n1) item\nexport GH_TOKEN=x\nEOF\n)\""
        "; gh pr create --body \"$BODY\"",
        False, id="s7c-body-heredoc-paren"),
    pytest.param(
        "x=\"$(cat <<EOF\nit's\nEOF\n)\""
        "; echo 'a; export GH_TOKEN=y; '; gh pr view",
        False, id="s8-heredoc-then-quoted-export"),
    pytest.param(
        "( export GH_TOKEN=x; cat <<< y\n)\ny\ngh pr view",
        False, id="s9-here-string-not-heredoc"),
    # #4433 flip: a heredoc / $( in gh args now routes (was explicit).
    pytest.param(
        "export GH_TOKEN=x; gh pr create --body "
        "\"$(cat <<'EOF'\nIt's\nEOF)\"",
        False, id="s10-export-then-body-heredoc"),
    # #4433 allowlist: anything beyond the two allowed shapes routes.
    pytest.param(
        "true | export GH_TOKEN=x; gh pr view",
        False, id="f1-pipe-before-export"),
    pytest.param(
        "gh api user; GH_TOKEN=y gh pr list",
        False, id="f2-gh-before-prefix"),
    pytest.param(
        "export GH_TOKEN=x; gh api a; gh api b",
        False, id="f3-second-gh-call"),
    pytest.param(
        "GH_TOKEN=x gh pr view | cat", False, id="f4-prefix-pipe"),
    pytest.param(
        "export GH_TOKEN=x && cd d && gh pr view",
        False, id="f5-extra-command-between"),
    # #4433 keep-explicit shapes.
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u) gh pr view 1",
        True, id="k1-prefix-auth-token-subst"),
    pytest.param(
        "export GH_TOKEN=$(cmd); gh pr view",
        True, id="k2-export-subst-value"),
    pytest.param(
        'export GH_TOKEN="x" && gh pr create --body b',
        True, id="k3-export-quoted-and-join"),
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
