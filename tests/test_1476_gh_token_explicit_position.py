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
    # #4447: inner $() allowlist, unterminated quote, empty value.
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u; gh pr merge 1) gh pr view 1",
        False, id="n1-inner-semicolon-merge"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u && gh pr merge 1) gh pr view 1",
        False, id="n2-inner-and-merge"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u || gh pr merge 1) gh pr view 1",
        False, id="n3-inner-or-merge"),
    pytest.param(
        "export GH_TOKEN=$(gh auth token | gh api -X DELETE repos/o/r); gh pr view",
        False, id="n4-inner-pipe-api-delete"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u \n gh pr merge 1) gh pr view 1",
        False, id="n5-inner-space-newline-tail"),
    pytest.param(
        "GH_TOKEN=x gh pr view 'abc",
        False, id="n6-unterminated-single-quote"),
    pytest.param(
        'GH_TOKEN=x gh pr view "abc',
        False, id="n7-unterminated-double-quote"),
    pytest.param(
        'GH_TOKEN="" gh pr view', "deny", id="n8-prefix-empty-double"),
    pytest.param(
        "GH_TOKEN='' gh pr view", "deny", id="n9-prefix-empty-single"),
    pytest.param(
        'export GH_TOKEN=""; gh pr view', "deny", id="n10-export-empty-double"),
    pytest.param(
        "export GH_TOKEN=''; gh pr view", "deny", id="n11-export-empty-single"),
    pytest.param(
        "GH_TOKEN= gh pr view", "deny", id="n12-prefix-empty-bare"),
    pytest.param(
        "export GH_TOKEN=; gh pr view", "deny", id="n13-export-empty-bare"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user lab-ovh) gh pr view 1",
        True, id="k4-prefix-auth-token-user"),
    pytest.param(
        "GH_TOKEN=$(gh auth token) gh api user",
        True, id="k5-prefix-auth-token-bare"),
    pytest.param(
        "export GH_TOKEN=$(gh auth token --hostname github.com --user u); gh pr view",
        True, id="k6-export-auth-token-hostname-user"),
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
    if explicit == "deny":
        output = json.loads(out)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "empty GH_TOKEN" in output["permissionDecisionReason"]
        assert "updatedInput" not in output
        return
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


def test_routed_token_reaches_gh_in_real_bash_with_stand_in_gh(
        tmp_path, monkeypatch, capsys):
    """Execute run_hook's rewrite in real bash; a stand-in gh logs its token."""
    import os
    import subprocess
    import sys

    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"lab-ovh": "lab-ovh"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    command = "GH_TOKEN=$(gh auth token --user u; gh pr merge 1) gh pr view 1"
    rc = gh_route.run_hook(json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }))
    assert rc == 0
    rewritten = json.loads(capsys.readouterr().out.strip())[
        "hookSpecificOutput"]["updatedInput"]["command"]
    assert rewritten.startswith(
        "export GH_TOKEN=$(gh auth token --user lab-ovh); ")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    stand_in = bin_dir / "gh"
    stand_in.write_text(
        "#!/bin/bash\n"
        'printf \'gh args=%s GH_TOKEN=%s\\n\' "$*" "${GH_TOKEN-}" >> "$STANDIN_GH_LOG"\n'
        'if [ "$1" = "auth" ] && [ "$2" = "token" ]; then\n'
        '  user="unknown"\n'
        '  while [ $# -gt 0 ]; do\n'
        '    if [ "$1" = "--user" ]; then user="$2"; fi\n'
        "    shift\n"
        "  done\n"
        '  printf \'standin-token-%s\\n\' "$user"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8")
    stand_in.chmod(0o755)
    if sys.platform == "win32":
        from swarph_cli.commands.hooks import _windows_hook_bash
        shell = _windows_hook_bash()
        if shell == "bash":
            pytest.skip("no non-WSL bash on this runner")
    else:
        shell = "bash"
    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["STANDIN_GH_LOG"] = str(log)
    result = subprocess.run(
        [shell, "-c", rewritten], capture_output=True, text=True,
        timeout=15, env=env)
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    merge_lines = [ln for ln in lines if "pr merge 1" in ln]
    view_lines = [ln for ln in lines if "pr view 1" in ln]
    assert merge_lines, lines
    assert view_lines, lines
    # The gh call inside the user's $() sees the router's non-empty token.
    assert "GH_TOKEN=standin-token-lab-ovh" in merge_lines[0]
    # The outer gh call sees a non-empty token, never the empty fallback.
    view_token = view_lines[0].rsplit("GH_TOKEN=", 1)[-1]
    assert view_token and view_token != ""
