"""Card #1555 — bash-aware shell tokenizer shared by targets_gh,
already_explicit, and _strip_allowlisted_gh_token.

Exercises each required shape through the real PreToolUse hook entry point
(``gh_route.run_hook``) on both a mapped cell (rewrites with export
GH_TOKEN=...) and an unmapped cell (denies):
  (a) "# Check what's open\\ngh pr list --json number -q '.[].number'" routes;
  (b) "# what's open\\ngh pr list\\n# that's it" routes;
  (c) 'GH_CONFIG_DIR=$(pwd)/.gh gh pr view 1', 'A=$(x)$(y) gh pr merge 1',
      'N=$((1+2)) gh pr view 1', and backtick env prefixes route;
  (d) 'GH_TOKEN=$(gh auth token) gh pr view 1 # note \\<LF>gh pr merge 2'
      is NOT treated as explicit for 'gh pr merge 2' (routes);
  (1) heredoc delimiters parsed as bash does (after quote removal: EOF-1,
      EOF.txt, E'O'F, \\EOF, -\\EOF, 1, 'END OF'), and unterminated or
      unclassifiable heredocs are DENIED;
  (2) heredocs and here-strings feeding a shell (bash, sh, bash -s, sh -s,
      with quoted or unquoted delimiter, or <<<) scan the body as shell text;
  (3) comments are never scanned ('# $(gh pr list)' and 'echo hi # $(gh ...)'
      get no token);
  (4) '!', 'env NAME=value' and a case arm 'pattern)' are command starts,
      and command words are compared after quote removal (g'h', g''h, g""h,
      g\\h, \\gh, $'gh');
  (5) any shape the tokenizer cannot classify is denied;
  (6) differential oracle against real bash + stub gh (no network) on fixed
      seeds 7 and 42 (1500 random + 142 hand cases each = 1642 cases/seed)
      with 0 escapes, 0 over-routes, and 0 regressions against main;
  (7) all 6 mutants fail a test.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import stat
import subprocess
import sys

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route


def _run_hook_for_cell(
    tmp_path, monkeypatch, capsys, command: str, *, peer: str
) -> dict | None:
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
    return json.loads(out) if out else None


def _assert_routes_through_hook(tmp_path, monkeypatch, capsys, command: str) -> None:
    mapped_out = _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, command, peer="lab-ovh")
    assert mapped_out is not None, f"expected hook output on mapped cell for: {command!r}"
    hso_mapped = mapped_out["hookSpecificOutput"]
    assert "updatedInput" in hso_mapped, (
        f"expected rewrite on mapped cell for {command!r}, got: {mapped_out!r}")
    rewritten = hso_mapped["updatedInput"]["command"]
    assert rewritten.startswith("export GH_TOKEN=$(gh auth token --user lab-ovh); ")
    assert rewritten.endswith(command)

    unmapped_out = _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, command, peer="unmapped-fixture-cell")
    assert unmapped_out is not None, (
        f"expected hook refusal on unmapped cell for: {command!r}")
    hso_unmapped = unmapped_out["hookSpecificOutput"]
    assert hso_unmapped.get("permissionDecision") == "deny"
    assert "unmapped-fixture-cell" in hso_unmapped.get("permissionDecisionReason", "")


def _assert_denied_through_hook(tmp_path, monkeypatch, capsys, command: str) -> None:
    for peer in ("lab-ovh", "unmapped-fixture-cell"):
        out = _run_hook_for_cell(tmp_path, monkeypatch, capsys, command, peer=peer)
        assert out is not None, f"expected DENY for {command!r} on {peer}"
        assert out["hookSpecificOutput"].get("permissionDecision") == "deny", (
            f"expected DENY for {command!r} on {peer}, got: {out!r}")


def _assert_unchanged_through_hook(tmp_path, monkeypatch, capsys, command: str) -> None:
    for peer in ("lab-ovh", "unmapped-fixture-cell"):
        out = _run_hook_for_cell(tmp_path, monkeypatch, capsys, command, peer=peer)
        assert out is None, f"expected UNCHANGED for {command!r} on {peer}, got: {out!r}"


def test_a_comment_apostrophe_before_quoted_gh_arg_routes(
        tmp_path, monkeypatch, capsys):
    """(a) '# Check what\\'s open\\ngh pr list --json number -q \\'.[].number\\'' routes."""
    cmd = "# Check what's open\ngh pr list --json number -q '.[].number'"
    assert ghid.targets_gh(cmd) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


def test_b_two_comment_apostrophes_bracketing_gh_line_routes(
        tmp_path, monkeypatch, capsys):
    """(b) '# what\\'s open\\ngh pr list\\n# that\\'s it' routes."""
    cmd = "# what's open\ngh pr list\n# that's it"
    assert ghid.targets_gh(cmd) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


@pytest.mark.parametrize("cmd", [
    pytest.param(
        "GH_CONFIG_DIR=$(pwd)/.gh gh pr view 1",
        id="c1-subst-with-suffix-in-env-prefix",
    ),
    pytest.param(
        "A=$(x)$(y) gh pr merge 1",
        id="c2-adjacent-substs-in-env-prefix",
    ),
    pytest.param(
        "N=$((1+2)) gh pr view 1",
        id="c3-arithmetic-subst-in-env-prefix",
    ),
    pytest.param(
        "GH_CONFIG_DIR=`pwd`/.gh gh pr view 1",
        id="c4-backtick-in-env-prefix",
    ),
    pytest.param(
        "N=$(( (1 + 2) * 3 )) A=`echo ok` gh pr view 1",
        id="c5-nested-arithmetic-and-backtick-with-spaces",
    ),
])
def test_c_substitutions_backticks_and_arithmetic_in_env_prefix_route(
        tmp_path, monkeypatch, capsys, cmd):
    """(c) $(...), backticks, and $((...)) inside an env-prefix value do not hide gh."""
    assert ghid.targets_gh(cmd) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


def test_d_backslash_newline_inside_comment_does_not_make_second_gh_explicit(
        tmp_path, monkeypatch, capsys):
    """(d) 'GH_TOKEN=$(gh auth token) gh pr view 1 # note \\<LF>gh pr merge 2'
    is NOT treated as explicit for 'gh pr merge 2': in bash a comment ends at
    <LF> regardless of a trailing backslash, so 'gh pr merge 2' is a second
    command and the hook must route it.
    """
    cmd = "GH_TOKEN=$(gh auth token) gh pr view 1 # note \\\ngh pr merge 2"
    assert ghid.targets_gh(cmd) is True
    assert ghid.has_disallowed_gh_token_assignment(cmd) is False
    assert ghid.already_explicit(cmd) is False
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


def test_hash_only_starts_comment_at_word_start(tmp_path, monkeypatch, capsys):
    """In bash, '#' starts a comment only at a word start; mid-word '#' is literal."""
    non_comment = "echo foo#bar 'gh pr list'"
    assert ghid.targets_gh(non_comment) is False
    _assert_unchanged_through_hook(tmp_path, monkeypatch, capsys, non_comment)

    assign_hash = "A=#notacomment gh pr view 1"
    assert ghid.targets_gh(assign_hash) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, assign_hash)


def test_heredoc_apostrophe_does_not_hide_later_gh_call(
        tmp_path, monkeypatch, capsys):
    """An apostrophe inside a heredoc body must not pair with a later quote."""
    cmd = "cat <<'EOF'\nCheck what's open\nEOF\ngh pr list --json number -q '.[].number'"
    assert ghid.targets_gh(cmd) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


def test_comment_with_backslash_newline_does_not_strip_disallowed_gh_token(
        tmp_path, monkeypatch, capsys):
    """_strip_allowlisted_gh_token shares the tokenizer: a comment cannot join
    across \\<LF> to manufacture an allowlisted GH_TOKEN=$(gh auth token) span.
    """
    cmd = "# GH_TOKEN=$(gh auth token \\\n)\ngh pr view 1"
    assert ghid.targets_gh(cmd) is True
    assert ghid.has_disallowed_gh_token_assignment(cmd) is True
    _assert_denied_through_hook(tmp_path, monkeypatch, capsys, cmd)


@pytest.mark.parametrize("cmd", [
    pytest.param("cat <<EOF-1\nit's\nEOF-1\ngh pr list -q '.[].number'", id="heredoc-EOF-1-apostrophe"),
    pytest.param("cat <<EOF-1\nbody\nEOF-1\ngh pr list", id="heredoc-EOF-1-plain"),
    pytest.param("cat <<EOF.txt\nit's\nEOF.txt\ngh pr list -q '.[].number'", id="heredoc-EOF.txt"),
    pytest.param("cat <<E'O'F\nit's\nEOF\ngh pr list -q '.[].number'", id="heredoc-partial-quote-E'O'F"),
    pytest.param("cat <<1\nit's\n1\ngh pr list -q '.[].number'", id="heredoc-digits-1"),
    pytest.param("cat <<'END OF'\nit's\nEND OF\ngh pr list -q '.[].number'", id="heredoc-spaced-END-OF"),
    pytest.param("cat <<\\EOF\nit's\nEOF\ngh pr list -q '.[].number'", id="heredoc-backslash-EOF"),
    pytest.param("cat <<-\\EOF\n\tit's\n\tEOF\ngh pr list -q '.[].number'", id="heredoc-dash-backslash-EOF"),
    pytest.param("cat <<$EOF\nit's\n$EOF\ngh pr list -q '.[].number'", id="heredoc-dollar-EOF"),
])
def test_1_bash_word_heredoc_delimiters_with_quote_removal_route(
        tmp_path, monkeypatch, capsys, cmd):
    """(1) Heredoc delimiters are parsed as bash words with quote removal."""
    assert ghid.targets_gh(cmd) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


@pytest.mark.parametrize("cmd", [
    pytest.param("cat <<EOF\nbody\ngh pr list", id="unterminated-heredoc-with-gh"),
    pytest.param("cat <<EOF\njust text no terminator", id="unterminated-heredoc-no-gh"),
    pytest.param("cat <<EOF-1\nEOF\ngh pr list", id="mismatched-terminator-EOF-vs-EOF-1"),
    pytest.param("cat <<'EOF\nbody\nEOF\ngh pr list", id="unclosed-quote-in-heredoc-delimiter"),
    pytest.param("cat <<$(echo EOF)\nbody\nEOF\ngh pr list", id="command-subst-in-heredoc-delimiter"),
])
def test_1_unterminated_or_unclassifiable_heredoc_is_denied(
        tmp_path, monkeypatch, capsys, cmd):
    """(1), (5), & #5142 (C): If the heredoc terminator is never found or
    delimiter is unclassifiable, DENY the command with its own heredoc reason
    (never the GH_TOKEN message, and never treat it as not-gh).
    """
    assert ghid.targets_gh(cmd) is True
    assert ghid.has_unterminated_heredoc(cmd) is True
    assert ghid.has_disallowed_gh_token_assignment(cmd) is False
    assert ghid.already_explicit(cmd) is False
    for peer in ("lab-ovh", "unmapped-fixture-cell"):
        out = _run_hook_for_cell(tmp_path, monkeypatch, capsys, cmd, peer=peer)
        assert out is not None, f"expected DENY for {cmd!r} on {peer}"
        hso = out["hookSpecificOutput"]
        assert hso.get("permissionDecision") == "deny"
        reason = hso.get("permissionDecisionReason", "")
        assert "heredoc" in reason.lower(), reason
        assert "GH_TOKEN" not in reason, reason


@pytest.mark.parametrize("cmd", [
    pytest.param("bash <<EOF\ngh pr list\nEOF", id="bash-heredoc-unquoted"),
    pytest.param("bash <<'EOF'\ngh pr list\nEOF", id="bash-heredoc-single-quoted"),
    pytest.param('bash <<"EOF"\ngh pr list\nEOF', id="bash-heredoc-double-quoted"),
    pytest.param("sh <<EOF\ngh pr list\nEOF", id="sh-heredoc"),
    pytest.param("sh -s <<EOF\ngh pr list\nEOF", id="sh-s-heredoc"),
    pytest.param("bash -s <<EOF\ngh pr list\nEOF", id="bash-s-heredoc"),
    pytest.param("bash -s <<'EOF-1'\ngh pr list\nEOF-1", id="bash-s-heredoc-EOF-1"),
    pytest.param("env bash <<'EOF'\ngh pr list\nEOF", id="env-bash-heredoc"),
    pytest.param("bash <<< 'gh pr list'", id="bash-herestring-sq"),
    pytest.param('bash <<< "gh pr view 1"', id="bash-herestring-dq"),
    pytest.param("sh -s <<< 'gh pr list'", id="sh-s-herestring"),
])
def test_2_heredoc_and_herestring_feeding_shell_scan_body_as_shell_text(
        tmp_path, monkeypatch, capsys, cmd):
    """(2) When a heredoc or here-string feeds a shell, scan the body as shell text."""
    assert ghid.targets_gh(cmd) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


@pytest.mark.parametrize("cmd", [
    pytest.param(
        "cat > x.sh <<'EOF'\ngh pr list\nEOF\nbash x.sh",
        id="cat-redir-before-heredoc-then-bash",
    ),
    pytest.param(
        "cat <<'EOF' > x.sh\n#!/bin/bash\ngh pr list\nEOF\nbash x.sh",
        id="cat-heredoc-before-redir-then-bash",
    ),
    pytest.param(
        "tee x.sh <<'EOF' >/dev/null\ngh pr list\nEOF\nsh x.sh",
        id="tee-heredoc-then-sh",
    ),
    pytest.param(
        "cat > f.sh <<'EOF'\nf() { gh pr list; }\nEOF\n. ./f.sh; f",
        id="cat-redir-then-dot-source",
    ),
    pytest.param(
        "cat > x.sh <<EOF\ngh pr list\nEOF\nchmod +x x.sh; ./x.sh",
        id="cat-redir-then-chmod-and-direct-exec",
    ),
    pytest.param(
        "cat >> x.sh <<'EOF'\ngh pr view 1\nEOF\nsource x.sh",
        id="cat-append-redir-then-source",
    ),
    pytest.param(
        "cat > s.py <<'EOF'\ngh pr list\nEOF\npython3 s.py",
        id="cat-redir-then-python3",
    ),
])
def test_2b_write_heredoc_to_path_then_run_routes(
        tmp_path, monkeypatch, capsys, cmd):
    """#5142 (A): When a heredoc is redirected to a path (>, >>, tee) and that
    path later appears in the same command as a command word or as an argument
    of bash/sh/zsh/python/./source, scan the heredoc body as shell text.
    """
    assert ghid.targets_gh(cmd) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


def test_2c_write_heredoc_to_path_without_running_does_not_route(
        tmp_path, monkeypatch, capsys):
    """Writing a heredoc to a file without executing it in the command stays inert."""
    for cmd in (
        "cat > x.sh <<'EOF'\ngh pr list\nEOF",
        "cat > x.sh <<'EOF'\ngh pr list\nEOF\nrm -f x.sh",
    ):
        assert ghid.targets_gh(cmd) is False
        _assert_unchanged_through_hook(tmp_path, monkeypatch, capsys, cmd)


@pytest.mark.parametrize("cmd", [
    pytest.param("# $(gh pr list)\necho hi", id="comment-subst-gh"),
    pytest.param("# `gh pr list`\necho hi", id="comment-backtick-gh"),
    pytest.param("# `gh pr list\necho hi", id="comment-unclosed-backtick-gh"),
    pytest.param("echo hi # $(gh pr list)", id="trailing-comment-subst-gh"),
    pytest.param("echo hi # `gh pr list`", id="trailing-comment-backtick-gh"),
    pytest.param("# $(gh pr list)", id="sole-comment-subst-gh"),
])
def test_3_never_scan_inside_comments(tmp_path, monkeypatch, capsys, cmd):
    """(3) Never scan inside comments: '# $(gh pr list)' and 'echo hi # $(gh ...)' get no token."""
    assert ghid.targets_gh(cmd) is False
    _assert_unchanged_through_hook(tmp_path, monkeypatch, capsys, cmd)


@pytest.mark.parametrize("cmd", [
    pytest.param("! gh pr list", id="bang-gh"),
    pytest.param("if ! gh pr view 1; then true; fi", id="if-bang-gh"),
    pytest.param("env A=1 gh pr list", id="env-A=1-gh"),
    pytest.param("env FOO=1 BAR=2 gh pr merge 1", id="env-FOO=1-BAR=2-gh"),
    pytest.param("env -i FOO=1 gh pr view 1", id="env-i-FOO=1-gh"),
    pytest.param("case a in a) gh pr list;; esac", id="case-arm-gh"),
    pytest.param("case a in (a) gh pr list;; esac", id="case-arm-paren-gh"),
    pytest.param("echo $(case a in a) gh pr list;; esac)", id="subst-case-arm-gh"),
    pytest.param("g'h' pr list", id="partly-single-quoted-gh"),
    pytest.param("g''h pr merge 1", id="empty-single-quote-join-gh"),
    pytest.param('g""h pr list', id="empty-double-quote-join-gh"),
    pytest.param(r"\gh pr list", id="leading-backslash-gh"),
    pytest.param(r"\gh pr merge 1", id="leading-backslash-gh-merge"),
    pytest.param(r"g\h pr merge 1", id="mid-backslash-gh"),
    pytest.param("$'gh' pr merge 1", id="ansi-c-quoted-gh"),
    pytest.param('$"gh" pr merge 1', id="locale-quoted-gh"),
    pytest.param("bash -c $'gh pr view 1'", id="bash-c-ansi-c-gh"),
    pytest.param("sh -c $'gh pr view 1'", id="sh-c-ansi-c-gh"),
    pytest.param("eval $'gh pr view 1'", id="eval-ansi-c-gh"),
    pytest.param(r"bash -c $'g\x68 pr view 1'", id="bash-c-ansi-c-hex-gh"),
    pytest.param(r"sh -c $'\147\150 pr view 1'", id="sh-c-ansi-c-octal-gh"),
    pytest.param(r"eval $'echo ok\ngh pr view 1'", id="eval-ansi-c-newline-gh"),
])
def test_4_command_starts_and_quote_removed_gh_words_route(
        tmp_path, monkeypatch, capsys, cmd):
    """(4) & #5142 (B): '!', 'env NAME=value', case arm 'pattern)', quote-removed
    gh words, and nested $'...' ANSI-C quoted shell strings route.
    """
    assert ghid.targets_gh(cmd) is True
    _assert_routes_through_hook(tmp_path, monkeypatch, capsys, cmd)


def test_mutant_1_comment_masking_off_is_caught(tmp_path, monkeypatch, capsys):
    """Mutant 1: turning comment-masking off in the shared tokenizer makes (a)
    and (b) misroute (targets_gh returns False and run_hook emits nothing).
    """
    monkeypatch.setattr(ghid, "_MASK_COMMENTS", False)
    cmd_a = "# Check what's open\ngh pr list --json number -q '.[].number'"
    cmd_b = "# what's open\ngh pr list\n# that's it"
    assert ghid.targets_gh(cmd_a) is False
    assert ghid.targets_gh(cmd_b) is False
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd_a, peer="lab-ovh") is None
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd_b, peer="lab-ovh") is None


def test_mutant_2_continuation_folded_inside_comments_is_caught(
        tmp_path, monkeypatch, capsys):
    """Mutant 2: folding backslash-LF inside comments makes (d) falsely pass
    already_explicit (so run_hook emits nothing and 'gh pr merge 2' runs unrouted).
    """
    monkeypatch.setattr(ghid, "_FOLD_CONTINUATIONS_IN_COMMENTS", True)
    cmd_d = "GH_TOKEN=$(gh auth token) gh pr view 1 # note \\\ngh pr merge 2"
    assert ghid.already_explicit(cmd_d) is True
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd_d, peer="lab-ovh") is None


def test_mutant_3_identifier_only_heredoc_delimiter_is_caught(
        tmp_path, monkeypatch, capsys):
    """Mutant 3: restricting heredoc delimiters to identifiers makes
    'cat <<EOF-1\\nit\\'s\\nEOF-1\\ngh pr list -q \\'.[].number\\'' fail to route.
    """
    monkeypatch.setattr(ghid, "_HEREDOC_IDENTIFIER_ONLY", True)
    cmd = "cat <<EOF-1\nit's\nEOF-1\ngh pr list -q '.[].number'"
    assert ghid.targets_gh(cmd) is False
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd, peer="lab-ovh") is None


def test_mutant_4_no_shell_heredoc_scan_is_caught(
        tmp_path, monkeypatch, capsys):
    """Mutant 4: disabling shell-heredoc / here-string scanning makes
    'bash <<EOF\\ngh pr list\\nEOF' and 'bash <<< \\'gh pr list\\'' fail to route.
    """
    monkeypatch.setattr(ghid, "_SCAN_SHELL_HEREDOC", False)
    cmd_hd = "bash <<EOF\ngh pr list\nEOF"
    cmd_hs = "bash <<< 'gh pr list'"
    assert ghid.targets_gh(cmd_hd) is False
    assert ghid.targets_gh(cmd_hs) is False
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd_hd, peer="lab-ovh") is None
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd_hs, peer="lab-ovh") is None


def test_mutant_5_comment_scanning_on_is_caught(
        tmp_path, monkeypatch, capsys):
    """Mutant 5: turning comment scanning on falsely routes '# $(gh pr list)\\necho hi'
    and 'echo hi # $(gh pr list)'.
    """
    monkeypatch.setattr(ghid, "_SCAN_COMMENTS_FOR_TARGETS", True)
    cmd_1 = "# $(gh pr list)\necho hi"
    cmd_2 = "echo hi # $(gh pr list)"
    assert ghid.targets_gh(cmd_1) is True
    assert ghid.targets_gh(cmd_2) is True
    out = _run_hook_for_cell(tmp_path, monkeypatch, capsys, cmd_1, peer="lab-ovh")
    assert out is not None and "updatedInput" in out["hookSpecificOutput"]


def test_mutant_6_no_bang_or_env_assign_prefix_is_caught(
        tmp_path, monkeypatch, capsys):
    """Mutant 6: disabling '!' and 'env NAME=value' prefixes makes '! gh pr list'
    and 'env A=1 gh pr list' fail to route.
    """
    monkeypatch.setattr(ghid, "_ENABLE_BANG_AND_ENV_ASSIGN", False)
    cmd_bang = "! gh pr list"
    cmd_env = "env A=1 gh pr list"
    assert ghid.targets_gh(cmd_bang) is False
    assert ghid.targets_gh(cmd_env) is False
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd_bang, peer="lab-ovh") is None
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd_env, peer="lab-ovh") is None


def test_mutant_7_no_write_then_run_scan_is_caught(
        tmp_path, monkeypatch, capsys):
    """Mutant 7 (#5142): disabling write-then-run heredoc scanning makes
    the 4 write-then-run repros fail to route.
    """
    monkeypatch.setattr(ghid, "_SCAN_WRITE_THEN_RUN", False)
    for cmd in (
        "cat > x.sh <<'EOF'\ngh pr list\nEOF\nbash x.sh",
        "cat <<'EOF' > x.sh\n#!/bin/bash\ngh pr list\nEOF\nbash x.sh",
        "tee x.sh <<'EOF' >/dev/null\ngh pr list\nEOF\nsh x.sh",
        "cat > f.sh <<'EOF'\nf() { gh pr list; }\nEOF\n. ./f.sh; f",
    ):
        assert ghid.targets_gh(cmd) is False
        assert _run_hook_for_cell(
            tmp_path, monkeypatch, capsys, cmd, peer="lab-ovh") is None


def test_mutant_8_no_ansi_c_resolution_is_caught(
        tmp_path, monkeypatch, capsys):
    """Mutant 8 (#5142): disabling $'...' resolution makes the 4 nested
    ANSI-C repros (and top-level $'gh') fail to route.
    """
    monkeypatch.setattr(ghid, "_RESOLVE_ANSI_C", False)
    for cmd in (
        "bash -c $'gh pr view 1'",
        "sh -c $'gh pr view 1'",
        "eval $'gh pr view 1'",
        r"bash -c $'g\x68 pr view 1'",
    ):
        assert ghid.targets_gh(cmd) is False
        assert _run_hook_for_cell(
            tmp_path, monkeypatch, capsys, cmd, peer="lab-ovh") is None


# ---------------------------------------------------------------------------
# Differential Oracle Test (#5129 / #5131 / #5142):
# Real bash + stub gh (no network), fixed seeds 7 and 42, >= 1500 random cases
# plus hand-written cases (including A and B shapes) per seed.
# ---------------------------------------------------------------------------

_EXPL = "GH_TOKEN=$(gh auth token --user bob)"

_ORACLE_HAND: dict[str, str] = {
    "a comment apostrophe":            "# Check what's open\ngh pr list --json number -q '.[].number'",
    "b two comment apostrophes":       "# what's open\ngh pr list\n# that's it",
    "c1 subst suffix in prefix":       "GH_CONFIG_DIR=$(pwd)/.gh gh pr view 1",
    "c2 adjacent substs":              "A=$(true)$(true) gh pr merge 1",
    "c3 arithmetic":                   "N=$((1+2)) gh pr view 1",
    "d comment backslash-LF":          "GH_TOKEN=$(gh auth token) gh pr view 1 # note \\\ngh pr merge 2",
    "heredoc plain data":              "cat <<EOF\ngh pr list\nEOF",
    "heredoc quoted apostrophe":       "cat <<'EOF'\nit's\nEOF\ngh pr list",
    "heredoc backslash delim":         "cat <<\\EOF\nit's\nEOF\ngh pr list",
    "heredoc dash backslash delim":    "cat <<-\\EOF\n\tit's\n\tEOF\ngh pr list",
    "heredoc dq delim":                "cat <<\"EOF\"\nit's\nEOF\ngh pr list",
    "heredoc hyphen delim":            "cat <<EOF-1\nit's\nEOF-1\ngh pr list",
    "heredoc dotted delim":            "cat <<EOF.txt\nit's\nEOF.txt\ngh pr list",
    "heredoc numeric delim":           "cat <<1\nit's\n1\ngh pr list",
    "heredoc spaced quoted delim":     "cat <<'END OF'\nit's\nEND OF\ngh pr list",
    "heredoc partial-quote delim":     "cat <<E'O'F\nit's\nEOF\ngh pr list",
    "heredoc dollar delim":            "cat <<$EOF\nit's\n$EOF\ngh pr list",
    "heredoc delim then space":        "cat << EOF\nit's\nEOF\ngh pr list",
    "heredoc body dq quote":           "cat <<EOF\nsay \"hi\nEOF\ngh pr list",
    "heredoc body backtick":           "cat <<'EOF'\na ` b\nEOF\ngh pr list",
    "heredoc body subst executes":     "cat <<EOF\n$(gh pr view 2)\nEOF",
    "heredoc quoted body subst data":  "cat <<'EOF'\n$(gh pr view 2)\nEOF",
    "heredoc body backtick executes":  "cat <<EOF\n`gh pr view 2`\nEOF",
    "heredoc fake terminator spaces":  "cat <<EOF\nEOF \ngh pr list\nEOF",
    "heredoc two on one line":         "cat <<A <<B\nit's\nA\nthat's\nB\ngh pr list",
    "heredoc then pipe to gh":         "cat <<EOF | gh pr comment 1 --body-file -\nit's\nEOF",
    "heredoc into bash":               "bash <<EOF\ngh pr list\nEOF",
    "heredoc into bash quoted":        "bash <<'EOF'\ngh pr list\nEOF",
    "heredoc into sh -s":              "sh -s <<EOF\ngh pr list\nEOF",
    "heredoc write then run 1":        "cat > x.sh <<'EOF'\ngh pr list\nEOF\nbash x.sh",
    "heredoc write then run 2":        "cat <<'EOF' > x.sh\n#!/bin/bash\ngh pr list\nEOF\nbash x.sh",
    "heredoc write then run 3":        "tee x.sh <<'EOF' >/dev/null\ngh pr list\nEOF\nsh x.sh",
    "heredoc write then run 4":        "cat > f.sh <<'EOF'\nf() { gh pr list; }\nEOF\n. ./f.sh; f",
    "heredoc write then run chmod":    "cat > x.sh <<EOF\ngh pr list\nEOF\nchmod +x x.sh; ./x.sh",
    "heredoc write only inert":        "cat > inert.sh <<'EOF'\ngh pr list\nEOF",
    "heredoc in subst":                "echo $(cat <<EOF\nit's\nEOF\n)\ngh pr list",
    "heredoc comment on op line":      "cat <<EOF # it's\nbody\nEOF\ngh pr list",
    "heredoc terminator in comment":   "cat <<EOF\nbody\n# EOF\ngh pr list\nEOF",
    "herestring data":                 "cat <<< 'gh pr list'",
    "herestring subst":                "cat <<< $(gh pr list)",
    "herestring apostrophe":           "cat <<< it\\'s\ngh pr list",
    "hash mid word":                   "echo foo#bar 'x'\ngh pr list",
    "hash after semicolon":            "echo a;# it's\ngh pr list",
    "hash after paren":                "(# it's\ngh pr list\n)",
    "hash after close paren":          "(true)# it's\ngh pr list",
    "hash after quote close":          "echo 'a'#b it's ok'\ngh pr list",
    "hash in subst":                   "echo $(# it's\ngh pr list\n)",
    "hash in backticks":               "echo `# it's\ngh pr list\n`",
    "hash special param":              "echo $# 'x'\ngh pr list",
    "hash special param apostrophe":   "set -- a; echo $# it\\'s\ngh pr list",
    "hash param length":               "X=abc; echo ${#X} 'x'\ngh pr list",
    "hash param strip":                "X=abc; echo ${X#a} ${X##*/} 'x'\ngh pr list",
    "hash param strip quote":          "X=abc; echo ${X#'} it's\ngh pr list",
    "hash arithmetic base":            "echo $((2#101)) 'x'\ngh pr list",
    "hash arithmetic base prefix":     "N=$((2#101)) gh pr view 1",
    "hash escaped":                    "echo \\# it\\'s\ngh pr list",
    "hash after escaped space":        "echo a\\ #b 'x'\ngh pr list",
    "hash after continuation":         "echo a \\\n# it's\ngh pr list",
    "hash after tab":                  "echo a\t# it's\ngh pr list",
    "hash after vertical tab":         "echo a\x0b#b 'x'\ngh pr list",
    "hash after nbsp":                 "echo a\u00a0#b 'x'\ngh pr list",
    "hash after cr":                   "echo a\r# it's\ngh pr list",
    "hash in dq":                      "echo \"a # it's\"\ngh pr list",
    "hash in dq multiline":            "echo \"a\n# it's\n\"\ngh pr list",
    "hash in sq multiline":            "echo 'a\n# \"q\n'\ngh pr list",
    "hash in bracket test":            "[[ a#b == a#b ]] && gh pr list",
    "hash assignment value":           "A=#notacomment gh pr view 1",
    "hash array":                      "a=(1 # it's\n2)\ngh pr list",
    "comment only gh":                 "# gh pr list",
    "comment gh then real":            "# gh pr list\necho hi",
    "comment with subst":              "# $(gh pr list)\necho hi",
    "comment with backtick":           "# `gh pr list\necho hi",
    "comment ends with backslash":     "# note \\\ngh pr list",
    "comment two backslashes":         "# note \\\\\ngh pr list",
    "crlf lines":                      "# what's open\r\ngh pr list\r\n",
    "ansi-c escaped quote":            "echo $'it\\'s' # c'omment\ngh pr list",
    "ansi-c then gh":                  "echo $'a\\'b'; gh pr list",
    "locale quote":                    "echo $\"it's\"; gh pr list",
    "dq escaped dq":                   "echo \"a\\\"b\"; gh pr list",
    "dq backslash end":                "echo \"a\\\\\"; gh pr list",
    "sq adjacent":                     "echo 'a''b'; gh pr list",
    "sq with backslash":               "echo 'a\\'; gh pr list",
    "quoted gh word":                  "'gh' pr list",
    "dq gh word":                      "\"gh\" pr list",
    "partly quoted gh":                "g'h' pr list",
    "escaped gh":                      "\\gh pr list",
    "gh in dq arg":                    "echo \"gh pr list\"",
    "gh in sq arg":                    "echo 'gh pr list'",
    "git commit mention":              "git commit -m \"fix; gh routing was wrong\"",
    "git commit sq mention":           "git commit -m 'gh pr list; gh pr view'",
    "subst in dq executes":            "echo \"$(gh pr list)\"",
    "subst in sq data":                "echo '$(gh pr list)'",
    "escaped subst in dq data":        "echo \"\\$(gh pr list)\"",
    "backtick in dq executes":         "echo \"`gh pr list`\"",
    "escaped backtick in dq data":     "echo \"\\`gh pr list\\`\"",
    "param default subst":             ": ${X:-$(gh pr list)}",
    "param default word":              "echo ${X:-gh} pr list",
    "nested subst":                    "echo $(echo $(gh pr list))",
    "subst with paren in quote":       "echo $(echo ')' ; gh pr list)",
    "subst case paren":                "echo $(case a in a) gh pr list;; esac)",
    "arith then gh":                   "echo $(( (1+2)*3 )); gh pr list",
    "process subst":                   "cat <(gh pr list)",
    "continuation in gh":              "gh \\\npr list",
    "continuation splits gh word":     "g\\\nh pr list",
    "continuation before gh":          "echo a; \\\ngh pr list",
    "continuation in sq is literal":   "echo 'a \\\n'; gh pr list",
    "continuation in dq":              "echo \"a \\\nb\"; gh pr list",
    "continuation after explicit":     _EXPL + " \\\ngh pr view 1",
    "explicit simple":                 _EXPL + " gh pr view 1",
    "explicit then second gh":         _EXPL + " gh pr view 1; gh pr merge 2",
    "explicit then newline gh":        _EXPL + " gh pr view 1\ngh pr merge 2",
    "explicit && second gh":           _EXPL + " gh pr view 1 && gh pr merge 2",
    "explicit on echo then gh":        _EXPL + " echo hi; gh pr list",
    "explicit in comment":             "# " + _EXPL + "\ngh pr list",
    "explicit in dq":                  "echo \"" + _EXPL + " gh pr view 1\"; gh pr list",
    "explicit in heredoc":             "cat <<'EOF'\n" + _EXPL + " gh pr view 1\nEOF\ngh pr list",
    "explicit pipe second gh":         _EXPL + " gh pr view 1 | gh pr comment 2 --body-file -",
    "explicit subshell second":        "(" + _EXPL + " gh pr view 1); gh pr merge 2",
    "explicit export":                 "export " + _EXPL + "; gh pr view 1; gh pr merge 2",
    "explicit export then unset":      "export " + _EXPL + "; unset GH_TOKEN; gh pr merge 2",
    "explicit export subshell only":   "(export " + _EXPL + "); gh pr merge 2",
    "explicit comment then cont":      _EXPL + " gh pr view 1 # it's \\\n; gh pr merge 2",
    "explicit empty":                  "GH_TOKEN= gh pr view 1",
    "explicit empty dq":               "GH_TOKEN=\"\" gh pr view 1",
    "explicit literal":                "GH_TOKEN=abc gh pr view 1",
    "explicit after hash word":        "echo a#b; " + _EXPL + " gh pr view 1; gh pr merge 2",
    "explicit trailing comment gh":    _EXPL + " gh pr view 1 # gh pr merge 2",
    "explicit comment then gh line":   _EXPL + " gh pr view 1 # x\ngh pr merge 2",
    "explicit wrapped env":            "env " + _EXPL + " gh pr view 1",
    "explicit GH_TOKEN in value":      "A=GH_TOKEN=$(gh auth token) gh pr view 1",
    "if then gh":                      "if true; then gh pr list; fi",
    "for do gh":                       "for i in 1; do gh pr list; done",
    "function gh":                     "f() { gh pr list; }; f",
    "brace group":                     "{ gh pr list; }",
    "pipe to gh":                      "echo x | gh pr comment 1 --body-file -",
    "background gh":                   "gh pr list &\nwait",
    "negated gh":                      "! gh pr list",
    "time gh":                         "time gh pr list",
    "command gh":                      "command gh pr list",
    "env gh":                          "env A=1 gh pr list",
    "bash -c gh":                      "bash -c 'gh pr list'",
    "bash -c with comment":            "bash -c '# it is\ngh pr list'",
    "bash -c ansi-c gh":               "bash -c $'gh pr view 1'",
    "sh -c ansi-c gh":                 "sh -c $'gh pr view 1'",
    "eval ansi-c gh":                  "eval $'gh pr view 1'",
    "bash -c ansi-c hex gh":           r"bash -c $'g\x68 pr view 1'",
    "eval gh":                         "eval 'gh pr list'",
    "eval dq gh":                      "eval \"gh pr list\"",
    "case gh":                         "case a in a) gh pr list;; esac",
    "assignment only named gh":        "gh=1; echo $gh",
    "word gh-foo":                     "gh-foo pr list; echo ghx",
    "path ending gh arg":              "echo /usr/bin/gh",
}

_ORACLE_GH = [
    "gh pr list", "gh pr view 1", "gh api user", "A=1 gh pr list",
    "A=$(true)/x gh pr view 1", "N=$((1+2)) gh pr view 1",
    "A=`true` gh pr list", "A='#' gh pr list", "A=# gh pr list",
    "echo $(gh pr list)", "echo `gh pr list`",
    "echo \"$(gh pr list)\"", "true | gh pr comment 1 --body-file -",
    "if true; then gh pr list; fi", "( gh pr list )",
    "{ gh pr list; }", "gh \\\npr list", _EXPL + " gh pr view 1",
    "GH_TOKEN=$(gh auth token) gh pr view 1",
    "cat <<EOF | gh pr comment 1 --body-file -\nit's\nEOF",
    "cat <<EOF\n$(gh pr view 2)\nEOF", "bash -c 'gh pr list'",
    "cat > x.sh <<'EOF'\ngh pr list\nEOF\nbash x.sh",
    "cat <<'EOF' > x.sh\n#!/bin/bash\ngh pr list\nEOF\nbash x.sh",
    "tee x.sh <<'EOF' >/dev/null\ngh pr list\nEOF\nsh x.sh",
    "cat > f.sh <<'EOF'\nf() { gh pr list; }\nEOF\n. ./f.sh; f",
    "bash -c $'gh pr view 1'",
    "sh -c $'gh pr view 1'",
    "eval $'gh pr view 1'",
    r"bash -c $'g\x68 pr view 1'",
]
_ORACLE_NON = [
    "echo hi", "true", "echo gh pr list", "echo 'gh pr list'",
    "echo \"gh pr list\"", "git commit -m \"fix; gh routing\"",
    "echo foo#bar", "echo 'it''s'", "echo \"a # b\"", "echo $'it\\'s'",
    "echo \\# x", "echo $#", "X=abc; echo ${#X} ${X#a}",
    "echo $((2#101))", "echo '$(gh pr list)'", "echo \"\\$(gh pr list)\"",
    "cat <<EOF\ngh pr list\nEOF", "cat <<'EOF'\nit's $(gh pr list)\nEOF",
    "cat <<\\EOF\nit's\nEOF", "cat <<-EOF\n\tit's\n\tEOF",
    "cat <<EOF-1\nit's\nEOF-1", "cat <<< 'gh pr list'", "echo a\\ #b",
    "echo \"x\\\"y\"", "echo 'a\\'", ": ${X:-gh}", "[[ a#b == a#b ]]",
    "echo a \\\nb",
    "cat > inert.sh <<'EOF'\ngh pr list\nEOF",
    "bash -c $'echo gh pr view 1'",
    r"sh -c $'echo g\x68'",
    "eval $'echo gh pr list'",
]
_ORACLE_COM = [
    "# note", "# what's open", "# say \"hi", "# gh pr list", "# tail \\",
    "# `gh", "# $(gh pr list)", "#", "# that's it",
    "# " + _EXPL + " gh pr view 1", "# it's \\\\", "#!x 'y",
]
_ORACLE_SEP = ["\n", "\n", "; ", " && ", "\n\n", " ;\n"]
_ORACLE_TRAIL = ["", "", "", " # ok", " # it's", " # x \\", " # \"q", " # `b"]


def _compose_oracle(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(1, 4)):
        r = rng.random()
        if r < 0.25:
            parts.append(("C", rng.choice(_ORACLE_COM)))
        elif r < 0.60:
            parts.append(("G", rng.choice(_ORACLE_GH)))
        else:
            parts.append(("N", rng.choice(_ORACLE_NON)))
    out = ""
    for i, (kind, text) in enumerate(parts):
        if i:
            prev = parts[i - 1]
            sep = (
                "\n"
                if (
                    prev[0] == "C"
                    or "<<" in prev[1]
                    or "<<" in text
                    or out.rstrip(" ").endswith(("\\", "#"))
                    or "#" in out.split("\n")[-1]
                )
                else rng.choice(_ORACLE_SEP)
            )
            out += sep
        out += text
        if kind != "C" and "<<" not in text and not text.endswith("\\"):
            out += rng.choice(_ORACLE_TRAIL)
    return out


def _find_real_bash() -> str | None:
    bash = shutil.which("bash")
    if not bash:
        return None
    if sys.platform == "win32" and "system32" in bash.lower():
        return None
    try:
        r = subprocess.run([bash, "-c", "echo ok"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or r.stdout.strip() != "ok":
            return None
    except Exception:
        return None
    return bash


def _setup_stub_dir(tmp_path: Path, bash_bin: str) -> Path:
    stub_dir = tmp_path / "stub_bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub_gh = stub_dir / "gh"
    stub_gh.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = auth ] && [ "$2" = token ]; then echo "AUTH $*" >> "$GH_LOG"; echo "TOKFIX"; exit 0; fi\n'
        'if [ -n "${GH_TOKEN:-}" ]; then t="set"; else t="unset"; fi\n'
        'echo "CALL $t $*" >> "$GH_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub_gh.chmod(stub_gh.stat().st_mode | stat.S_IEXEC)
    for util in ("bash", "sh", "cat", "echo", "true", "false", "env", "pwd", "git", "time", "tee", "chmod"):
        target = shutil.which(util)
        if target and not (stub_dir / util).exists():
            try:
                (stub_dir / util).symlink_to(target)
            except OSError:
                shutil.copy2(target, stub_dir / util)
    return stub_dir


def _hook_decision_via_run_hook(cmd: str) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh_route.run_hook(json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }))
    assert rc == 0
    raw = buf.getvalue().strip()
    if not raw:
        return "UNCHANGED_EXPLICIT" if ghid.already_explicit(cmd) else "UNCHANGED_NOT_GH"
    hso = json.loads(raw)["hookSpecificOutput"]
    if hso.get("permissionDecision") == "deny":
        return "DENY"
    updated = hso.get("updatedInput", {}).get("command", "")
    if updated.startswith("export GH_TOKEN=$(gh auth token --user "):
        return "INJECT"
    return "OTHER"


def _oracle_verdict(decision: str, rc: int | str, calls: list[str]) -> str:
    ambient = [line for line in calls if line.startswith("CALL unset")]
    any_gh = bool(calls)
    if decision == "UNCHANGED_NOT_GH" and any_gh:
        return "ESCAPE"
    if decision == "UNCHANGED_EXPLICIT" and ambient:
        return "ESCAPE"
    if decision == "INJECT" and not any_gh and rc == 0:
        return "OVER-ROUTE"
    return "ok"


@pytest.mark.parametrize("seed", [7, 42])
def test_6_bash_stub_gh_oracle_zero_escapes_overroutes_or_regressions(
        tmp_path, monkeypatch, seed):
    """(6) Ship drop's differential oracle as a test: hook decision through
    run_hook against real bash with a stub gh, no network, fixed seeds 7 and
    42, 1500 random composed cases + 152 hand cases each = 1652 cases/seed.
    Asserts 0 escapes, 0 over-routes, and 0 regressions against main.
    """
    bash_bin = _find_real_bash()
    if bash_bin is None:
        pytest.skip("real POSIX bash not available on this runner")

    stub_dir = _setup_stub_dir(tmp_path, bash_bin)
    work_dir = tmp_path / f"work_{seed}"
    work_dir.mkdir(parents=True, exist_ok=True)

    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"lab-ovh": "lab-ovh"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    rng = random.Random(seed)
    cases: list[tuple[str, str]] = [("hand:" + k, v) for k, v in _ORACLE_HAND.items()]
    seen = {v for _, v in cases}
    while len(cases) < len(_ORACLE_HAND) + 1500:
        c = _compose_oracle(rng)
        if c not in seen:
            seen.add(c)
            cases.append(("rand", c))

    assert len(cases) >= 1650

    def eval_truth(idx_and_case: tuple[int, tuple[str, str]]):
        idx, (name, cmd) = idx_and_case
        case_dir = work_dir / f"c_{idx}"
        case_dir.mkdir(parents=True, exist_ok=True)
        log_file = case_dir / "gh.log"
        log_file.write_text("", encoding="utf-8")
        env = {
            "PATH": str(stub_dir),
            "HOME": str(case_dir),
            "GH_LOG": str(log_file),
        }
        try:
            p = subprocess.run(
                [str(stub_dir / "bash"), "-c", cmd],
                env=env,
                cwd=str(case_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            rc: int | str = p.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
        calls = [
            line.rstrip("\n")
            for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        ]
        shutil.rmtree(case_dir, ignore_errors=True)
        return name, cmd, rc, calls

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(eval_truth, enumerate(cases)))

    escapes = []
    over_routes = []
    other_bad = []
    for name, cmd, rc, calls in results:
        decision = _hook_decision_via_run_hook(cmd)
        v = _oracle_verdict(decision, rc, calls)
        if v == "ESCAPE":
            escapes.append((name, cmd, decision, rc, calls))
        elif v == "OVER-ROUTE":
            over_routes.append((name, cmd, decision, rc, calls))
        elif v != "ok" or decision == "OTHER":
            other_bad.append((name, cmd, decision, rc, calls))

    assert not escapes, f"seed={seed} had {len(escapes)} ESCAPEs: {escapes[:5]}"
    assert not over_routes, f"seed={seed} had {len(over_routes)} OVER-ROUTEs: {over_routes[:5]}"
    assert not other_bad, f"seed={seed} had {len(other_bad)} unexpected decisions: {other_bad[:5]}"
