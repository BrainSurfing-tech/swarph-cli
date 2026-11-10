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
  (e) word-start '#' boundary, heredoc apostrophes, and the two required
      mutants (comment-masking off; continuation folded inside comments).
"""
from __future__ import annotations

import json

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
    # Mid-word '#' before an apostrophe is NOT a comment, so the single quote
    # is a real quote opening and pairs with the closing quote on the same line.
    non_comment = "echo foo#bar 'gh pr list'"
    assert ghid.targets_gh(non_comment) is False
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, non_comment, peer="lab-ovh") is None

    # '#' inside an assignment value (A=#val) is mid-word, so the following
    # 'gh pr view 1' on the SAME line is still executed and must route!
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
    out = _run_hook_for_cell(tmp_path, monkeypatch, capsys, cmd, peer="lab-ovh")
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_mutant_comment_masking_off_is_caught(tmp_path, monkeypatch, capsys):
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


def test_mutant_continuation_folded_inside_comments_is_caught(
        tmp_path, monkeypatch, capsys):
    """Mutant 2: folding backslash-LF inside comments makes (d) falsely pass
    already_explicit (so run_hook emits nothing and 'gh pr merge 2' runs unrouted).
    """
    monkeypatch.setattr(ghid, "_FOLD_CONTINUATIONS_IN_COMMENTS", True)
    cmd_d = "GH_TOKEN=$(gh auth token) gh pr view 1 # note \\\ngh pr merge 2"
    assert ghid.already_explicit(cmd_d) is True
    assert _run_hook_for_cell(
        tmp_path, monkeypatch, capsys, cmd_d, peer="lab-ovh") is None
