"""Card #1346: a gh inside a shell-executing string is a gh call.

bash/sh/zsh/dash -c, eval, watch, and xargs ... sh -c run the string they are
given. A quoted command word whose value is gh is gh. A commit message that
mentions those words is not. gh-route doctor --quiet is one line when every
cell resolves, and the plugin SessionStart hook uses that flag.
"""
from __future__ import annotations

import json

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route


@pytest.mark.parametrize("cmd", [
    'bash -c "gh pr view 1"',
    "sh -c 'gh pr view 1'",
    'zsh -c "gh pr view 1"',
    'dash -c "gh pr view 1"',
    'eval "gh pr view 1"',
    'watch -n 5 "gh pr view 1"',
    "xargs -I{} sh -c 'gh pr view {}'",
    'timeout 20 bash -c "gh pr checks 1"',
    '"gh" pr view 1',
    "'gh' pr list",
])
def test_shell_string_and_quoted_command_word_are_gh(cmd):
    assert ghid.targets_gh(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "gh pr view 1",
    "cd /tmp && gh pr list",
    "foo; gh pr list",
    'echo "$(gh pr list)"',
    "if gh pr view 249; then echo yes; fi",
])
def test_existing_matches_keep_their_answers(cmd):
    """Real invocations stay matched. The quoted commit does not: card #1340."""
    assert ghid.targets_gh(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "echo 'gh is a tool'",
    'python3 -c "print(\'gh\')"',
    "grep gh file.txt",
    "ls /usr/bin/gh",
    'git commit -m "fix; gh routing was wrong"',
    'git commit -m "note: bash -c gh is mentioned"',
    'bash -c "echo \'gh is a tool\'"',
    "xargs echo sh -c 'gh pr list'",
])
def test_a_mention_is_not_a_shell_that_runs_gh(cmd):
    assert not ghid.targets_gh(cmd), cmd


def test_nested_shell_strings_match_and_a_deep_one_does_not_raise():
    inner = "gh pr list"
    for _ in range(4):
        inner = "bash -c " + json.dumps(inner)
    assert ghid.targets_gh(inner)
    # A long quoted string and a deep quote run return. They must not raise.
    ghid.targets_gh("bash -c " + json.dumps("x" * 1_000_000))
    ghid.targets_gh("(" * 20_000 + "gh pr list" + ")" * 20_000)


def test_hostile_strings_return_without_raising():
    ghid.targets_gh('bash -c "gh pr')
    ghid.targets_gh("bash -c 'gh")
    ghid.targets_gh("bash -c " + "\\")
    ghid.targets_gh('bash -c "' + ("x" * 1_000_000) + '"')
    ghid.targets_gh('"' * 100_000)


def test_unmapped_cell_is_denied_for_a_shell_string(tmp_path, monkeypatch):
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"lab-ovh": "lab-ovh"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "meta-muse")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh_route.run_hook(json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": 'bash -c "gh pr view 1"'},
        }))
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_doctor_quiet_is_one_line_when_every_cell_resolves(tmp_path, monkeypatch, capsys):
    mapping = tmp_path / "map.json"
    mapping.write_text(json.dumps({
        "lab-ovh": "orchestrators-hue",
        "cursor-lin": "cursor-lin-gh",
    }), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setattr(gh_route, "_cells_on_this_box", lambda: ["cursor-lin", "lab-ovh"])
    rc = gh_route.run_doctor(quiet=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines() == [
        "all 2 cells on this box resolve — safe to install"
    ]


def test_doctor_quiet_prints_the_full_table_when_one_cell_is_refused(
        tmp_path, monkeypatch, capsys):
    mapping = tmp_path / "map.json"
    mapping.write_text(json.dumps({"lab-ovh": "orchestrators-hue"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    monkeypatch.setattr(
        gh_route, "_cells_on_this_box", lambda: ["cursor-lin", "lab-ovh"])
    rc = gh_route.run_doctor(quiet=True)
    out = capsys.readouterr().out
    assert rc != 0
    assert "cursor-lin" in out
    assert "REFUSED" in out
    assert "1 of 2" in out
    assert len(out.splitlines()) > 1
