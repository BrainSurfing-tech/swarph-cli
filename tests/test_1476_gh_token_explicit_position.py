"""Card #1476 — residual GH_TOKEN denies (#4479, on the #4467 allowlist).

run_hook, before any rewrite, for every command targets_gh accepts:
strip each exact ``[export ]GH_TOKEN=$(gh auth token [--user W]
[--hostname W])`` (W a plain word) when it is followed by whitespace,
``;``, ``&``, ``|``, a newline, or end of text. If ``GH_TOKEN`` remains
anywhere, DENY on both cells with a reason that names ``--body-file``
and that explicit form. Exercised through run_hook with stdin JSON.
Routed means rewrite for mapped and deny for unmapped; explicit means
the hook leaves the call unchanged.
"""
import json

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route

CASES = [
    pytest.param(
        "cat <<'EOF'\nGH_TOKEN=x\nEOF\ngh api user", "deny", id="a-heredoc"),
    pytest.param("GH_TOKEN=$(cmd) gh pr view", "deny", id="b-prefix"),
    pytest.param("export GH_TOKEN=x; gh pr view", "deny", id="c-export-before"),
    pytest.param("echo GH_TOKEN=x; gh pr view", "deny", id="d-echo-arg"),
    pytest.param("gh api x; export GH_TOKEN=y", "deny", id="e-export-after"),
    # #4423 shapes: a GH_TOKEN mention denies on both cells (#4479).
    pytest.param(
        "cat <<'EOF'\nit's\nEOF\ncat <<'B'\nexport GH_TOKEN=x\nB\ngh pr view",
        "deny", id="s1-heredoc-quote-then-heredoc-export"),
    pytest.param(
        "cat <<'EOF'\nit's\nEOF\necho 'x; export GH_TOKEN=y; '; gh pr view",
        "deny", id="s2-heredoc-quote-then-quoted-export"),
    pytest.param(
        "cat <<'EOF'\nEOF \nexport GH_TOKEN=x\nEOF\ngh pr view",
        "deny", id="s3a-terminator-trailing-space"),
    pytest.param(
        "cat <<'EOF'\n EOF\nexport GH_TOKEN=x\nEOF\ngh pr view",
        "deny", id="s3b-terminator-leading-space"),
    pytest.param(
        "echo $'a\\'; export GH_TOKEN=x; \\''; gh pr view",
        "deny", id="s4-ansi-c-escaped-quote"),
    pytest.param(
        "export -n GH_TOKEN=x; gh pr view", "deny", id="s5-export-option"),
    pytest.param(
        "export GH_TOKEN=x | gh pr view", "deny", id="s6a-export-piped"),
    pytest.param(
        "export GH_TOKEN=x & gh pr view", "deny", id="s6b-export-backgrounded"),
    # #4426 shapes: heredoc inside $( inside double quotes. The heredoc
    # body is raw text: its apostrophes and ) never close the quote or
    # the substitution, and an export in the body is not explicit.
    pytest.param(
        "BODY=\"$(cat <<'EOF'\nIt's\n1) item\nexport GH_TOKEN=x\nEOF\n)\""
        "; gh pr create --body \"$BODY\"",
        "deny", id="s7-body-heredoc-paren-apostrophe"),
    pytest.param(
        "BODY=\"$(cat <<'EOF'\nIt's\nexport GH_TOKEN=x\nEOF\n)\""
        "; gh pr create --body \"$BODY\"",
        "deny", id="s7b-body-heredoc-apostrophe"),
    pytest.param(
        "BODY=\"$(cat <<'EOF'\nIts\n1) item\nexport GH_TOKEN=x\nEOF\n)\""
        "; gh pr create --body \"$BODY\"",
        "deny", id="s7c-body-heredoc-paren"),
    pytest.param(
        "x=\"$(cat <<EOF\nit's\nEOF\n)\""
        "; echo 'a; export GH_TOKEN=y; '; gh pr view",
        "deny", id="s8-heredoc-then-quoted-export"),
    pytest.param(
        "( export GH_TOKEN=x; cat <<< y\n)\ny\ngh pr view",
        "deny", id="s9-here-string-not-heredoc"),
    # #4433 flip: a heredoc / $( in gh args now routes (was explicit).
    pytest.param(
        "export GH_TOKEN=x; gh pr create --body "
        "\"$(cat <<'EOF'\nIt's\nEOF)\"",
        "deny", id="s10-export-then-body-heredoc"),
    # #4467: a non-allowlisted assignment denies in any composed shape.
    pytest.param(
        "true | export GH_TOKEN=x; gh pr view",
        "deny", id="f1-pipe-before-export"),
    pytest.param(
        "gh api user; GH_TOKEN=y gh pr list",
        "deny", id="f2-gh-before-prefix"),
    pytest.param(
        "export GH_TOKEN=x; gh api a; gh api b",
        "deny", id="f3-second-gh-call"),
    pytest.param(
        "GH_TOKEN=x gh pr view | cat", "deny", id="f4-prefix-pipe"),
    pytest.param(
        "export GH_TOKEN=x && cd d && gh pr view",
        "deny", id="f5-extra-command-between"),
    # #4433 keep-explicit shapes.
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u) gh pr view 1",
        True, id="k1-prefix-auth-token-subst"),
    pytest.param(
        "export GH_TOKEN=$(cmd); gh pr view",
        "deny", id="k2-export-subst-value"),
    pytest.param(
        'export GH_TOKEN="x" && gh pr create --body b',
        "deny", id="k3-export-quoted-and-join"),
    # #4447: inner $() allowlist, unterminated quote, empty value.
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u; gh pr merge 1) gh pr view 1",
        "deny", id="n1-inner-semicolon-merge"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u && gh pr merge 1) gh pr view 1",
        "deny", id="n2-inner-and-merge"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u || gh pr merge 1) gh pr view 1",
        "deny", id="n3-inner-or-merge"),
    pytest.param(
        "export GH_TOKEN=$(gh auth token | gh api -X DELETE repos/o/r); gh pr view",
        "deny", id="n4-inner-pipe-api-delete"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u \n gh pr merge 1) gh pr view 1",
        "deny", id="n5-inner-space-newline-tail"),
    pytest.param(
        "GH_TOKEN=x gh pr view 'abc",
        "deny", id="n6-unterminated-single-quote"),
    pytest.param(
        'GH_TOKEN=x gh pr view "abc',
        "deny", id="n7-unterminated-double-quote"),
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
    # #4459: statically empty command substitution is an empty value.
    pytest.param(
        "GH_TOKEN=$() gh pr view 1", "deny", id="n14-prefix-empty-subst"),
    pytest.param(
        "GH_TOKEN=$( ) gh pr view 1", "deny", id="n15-prefix-empty-subst-space"),
    pytest.param(
        "export GH_TOKEN=$(); gh pr view 1", "deny", id="n16-export-empty-subst"),
    pytest.param(
        "export GH_TOKEN=$( ); gh pr view 1", "deny",
        id="n17-export-empty-subst-space"),
    pytest.param(
        "export GH_TOKEN=$() && gh pr view 1", "deny",
        id="n18-export-empty-subst-and"),
    pytest.param(
        "export GH_TOKEN=$()\ngh pr view 1", "deny",
        id="n19-export-empty-subst-newline"),
    pytest.param(
        "export GH_TOKEN=$( ) && gh pr view 1", "deny",
        id="n20-export-empty-subst-space-and"),
    pytest.param(
        "export GH_TOKEN=$( )\ngh pr view 1", "deny",
        id="n21-export-empty-subst-space-newline"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user lab-ovh) gh pr view 1",
        True, id="k4-prefix-auth-token-user"),
    pytest.param(
        "GH_TOKEN=$(gh auth token) gh api user",
        True, id="k5-prefix-auth-token-bare"),
    pytest.param(
        "export GH_TOKEN=$(gh auth token --hostname github.com --user u); gh pr view",
        True, id="k6-export-auth-token-hostname-user"),
    pytest.param(
        "export GH_TOKEN=$(gh auth token --user u); gh pr view",
        True, id="k7-export-auth-token-user"),
    # #4467 drop's 12 shapes: each DENY on both cells.
    pytest.param('GH_TOKEN=""; gh pr view', "deny", id="d1-bare-empty-double"),
    pytest.param("GH_TOKEN=; gh pr view", "deny", id="d2-bare-empty-bare"),
    pytest.param(
        "declare -x GH_TOKEN=; gh pr view", "deny", id="d3-declare-empty"),
    pytest.param(
        "typeset GH_TOKEN=; gh pr view", "deny", id="d4-typeset-empty"),
    pytest.param(
        "readonly GH_TOKEN=; gh pr view", "deny", id="d5-readonly-empty"),
    pytest.param(
        'GH_TOKEN="""" gh pr view', "deny", id="d6-double-double-quotes"),
    pytest.param(
        "GH_TOKEN=''\"\" gh pr view", "deny", id="d7-single-double-quotes"),
    pytest.param("GH_TOKEN=$'' gh pr view", "deny", id="d8-ansi-empty-single"),
    pytest.param('GH_TOKEN=$"" gh pr view', "deny", id="d9-dollar-empty-double"),
    pytest.param('GH_TOKEN="$()" gh pr view', "deny", id="d10-quoted-empty-subst"),
    pytest.param(
        "echo `GH_TOKEN=x`; gh pr view", "deny", id="d11-backtick-segment"),
    pytest.param(
        "GH_TOKEN=x \\\ngh pr view", "deny", id="d12-backslash-newline-before-gh"),
    pytest.param(
        "local GH_TOKEN=x; gh pr view", "deny", id="d13-local-form"),
    pytest.param(
        "declare GH_TOKEN=x; gh pr view", "deny", id="d14-declare-value"),
    # Quoted / comment mentions stay routed (unchanged).
    pytest.param(
        'echo "GH_TOKEN=x"; gh pr view', "deny", id="q1-double-quoted-mention"),
    pytest.param(
        "echo 'GH_TOKEN=x'; gh pr view", "deny", id="q2-single-quoted-mention"),
    pytest.param(
        "# GH_TOKEN=x\ngh pr view", "deny", id="q3-comment-mention"),
    # drop #4469: an earlier substitution or a shell string hid the deny.
    pytest.param(
        "A=$(x) B=1 GH_TOKEN= gh pr view", "deny",
        id="p1-subst-assign-then-empty"),
    pytest.param(
        "A=$(x; y) GH_TOKEN= gh pr view", "deny",
        id="p2-subst-semi-then-empty"),
    pytest.param(
        'A=$(x|y) GH_TOKEN="" gh pr view', "deny",
        id="p3-subst-pipe-then-empty"),
    pytest.param(
        "GH_TOKEN=$(gh auth token --user u) GH_TOKEN= gh pr view", "deny",
        id="p4-double-token-before-gh"),
    pytest.param(
        "bash -c 'GH_TOKEN= gh pr view'", "deny",
        id="p5-bash-c-empty-prefix"),
    pytest.param(
        'sh -c "GH_TOKEN=\\"\\" gh pr view"', "deny",
        id="p6-sh-c-escaped-empty"),
    pytest.param(
        "eval 'GH_TOKEN=; gh pr view'", "deny", id="p7-eval-empty"),
    pytest.param(
        "bash -c 'GH_TOKEN=; gh pr view'", "deny",
        id="p8-bash-c-empty-statement"),
    pytest.param(
        "bash -c 'export GH_TOKEN=; gh pr view'", "deny",
        id="p9-bash-c-export-empty"),
    pytest.param(
        "xargs sh -c 'GH_TOKEN=; gh pr view'", "deny",
        id="p10-xargs-sh-c-empty"),
    # gpu-wsl #4468: $( / ${ before an empty GH_TOKEN, and a second GH_TOKEN.
    pytest.param(
        "GH_TOKEN=$(gh auth token --user lab-ovh) GH_TOKEN=$() gh pr view 1",
        "deny", id="g1-auth-then-empty-subst"),
    pytest.param(
        'GH_TOKEN=$(gh auth token) GH_TOKEN="" gh pr view 1', "deny",
        id="g2-auth-then-empty-double"),
    pytest.param(
        'X=${Y} GH_TOKEN="" gh pr view 1', "deny", id="g3-dollar-brace"),
    pytest.param(
        'X=${Y:-a} GH_TOKEN="" gh pr view 1', "deny",
        id="g4-dollar-brace-default"),
    pytest.param(
        'X={a} GH_TOKEN="" gh pr view 1', "deny", id="g5-brace-assign"),
    pytest.param(
        'X=$(echo a) GH_TOKEN="" gh pr view 1', "deny",
        id="g6-echo-subst"),
    pytest.param(
        "X=$(echo a) Z=2 GH_TOKEN= gh pr view 1", "deny",
        id="g7-echo-subst-two-assign"),
    pytest.param(
        'X=$(echo a) declare -x GH_TOKEN=""; gh pr view 1', "deny",
        id="g8-declare-after-subst"),
    pytest.param(
        'export X=$(echo a) GH_TOKEN=""; gh pr view 1', "deny",
        id="g9-export-subst-then-empty"),
    pytest.param(
        'declare -x X=${Y} GH_TOKEN=""; gh pr view 1', "deny",
        id="g10-declare-dollar-brace"),
    pytest.param(
        "bash -c 'unset GH_TOKEN; gh pr view'", "deny",
        id="u1-bash-c-unset"),
    pytest.param(
        "env -u GH_TOKEN gh pr view", "deny", id="u2-env-unset"),
    pytest.param(
        'gh pr create --body "uses GH_TOKEN"', "deny",
        id="u3-body-mentions-token"),
    # Routed: no residual GH_TOKEN after the exact form is stripped.
    pytest.param("gh pr view 1", False, id="r-plain-gh-view"),
    pytest.param(
        "GH_TOKEN=$(gh auth token) gh pr view 'abc", False,
        id="m17-allowlisted-unterminated-single"),
    pytest.param(
        'GH_TOKEN=$(gh auth token --user u) gh pr view "abc', False,
        id="m17-allowlisted-user-unterminated-double"),
    pytest.param(
        "export GH_TOKEN=$(gh auth token); gh pr view 'abc", False,
        id="m17-export-allowlisted-unterminated-single"),
    pytest.param(
        "GH_TOKEN=$(gh auth token) gh pr view 1 'abc", False,
        id="m17-allowlisted-then-unterminated-quote"),
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
        reason = output["permissionDecisionReason"]
        assert "--body-file" in reason
        assert "GH_TOKEN=$(gh auth token" in reason
        assert "\n" not in reason
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


def test_non_allowlisted_prefix_is_denied_not_rewritten(
        tmp_path, monkeypatch, capsys):
    """#4467: GH_TOKEN=$(...; gh pr merge) is not allowlisted, so deny."""
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
    output = json.loads(capsys.readouterr().out.strip())["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "--body-file" in output["permissionDecisionReason"]
    assert "GH_TOKEN=$(gh auth token" in output["permissionDecisionReason"]
    assert "updatedInput" not in output


def test_targets_gh_env_prefix_no_redos():
    """#4467 blocker 2: disjoint _ENVPFX alternatives stay linear."""
    import time

    command = "A=$(x) " * 22
    start = time.perf_counter()
    result = ghid.targets_gh(command)
    elapsed = time.perf_counter() - start
    assert result is False
    assert elapsed < 0.1, f"targets_gh took {elapsed:.3f}s"


def test_targets_gh_sudo_option_arg_no_redos():
    """#4469: a sudo option argument must not start with '-', so flags stay linear."""
    import time

    command = "sudo" + (" -a" * 30) + " zz"
    start = time.perf_counter()
    result = ghid.targets_gh(command)
    elapsed = time.perf_counter() - start
    assert result is False
    assert elapsed < 0.1, f"targets_gh took {elapsed:.3f}s"


def test_routed_token_reaches_gh_in_real_bash_with_stand_in_gh(
        tmp_path, monkeypatch, capsys):
    """Allowlisted value plus an unterminated quote routes; a stand-in gh sees the token.

    run_hook rewrites the command. Bash cannot parse the open quote, so the
    executed copy closes that one quote and nothing else. The user's
    ``$(gh auth token)`` then runs under the router's export.
    """
    import os
    import subprocess
    import sys

    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"lab-ovh": "lab-ovh"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    command = "GH_TOKEN=$(gh auth token) gh pr view 'abc"
    rc = gh_route.run_hook(json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }))
    assert rc == 0
    rewritten = json.loads(capsys.readouterr().out.strip())[
        "hookSpecificOutput"]["updatedInput"]["command"]
    assert rewritten.startswith(
        "export GH_TOKEN=$(gh auth token --user lab-ovh); ")
    assert rewritten.endswith(command)

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
        [shell, "-c", rewritten + "'"], capture_output=True, text=True,
        timeout=15, env=env)
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    user_auth = [ln for ln in lines if "auth token" in ln and "--user" not in ln]
    view_lines = [ln for ln in lines if "pr view" in ln]
    assert user_auth, lines
    assert view_lines, lines
    assert "GH_TOKEN=standin-token-lab-ovh" in user_auth[0]
    view_token = view_lines[0].rsplit("GH_TOKEN=", 1)[-1]
    assert view_token
