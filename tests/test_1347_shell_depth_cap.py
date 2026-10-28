"""Card #1347: at the shell-string depth cap the matcher answers True.

A cap that returns False is a bypass: 33 nested eval or watch words around gh
go unrouted. True routes the call, or denies it on an unmapped cell. The known
unrouted idioms stay unrouted and are named in the router docstring.
"""
from __future__ import annotations

import json
import time

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route


def _nested(word: str, n: int) -> str:
    return (word + " ") * n + '"gh pr view 1"'


@pytest.mark.parametrize("n", [33, 64, 1000])
@pytest.mark.parametrize("word", ["eval", "watch"])
def test_past_the_cap_still_matches_gh(word, n):
    assert ghid.targets_gh(_nested(word, n))


def test_thirty_two_still_matches_by_reaching_gh():
    assert ghid.targets_gh(_nested("eval", 32))
    assert ghid.targets_gh(_nested("watch", 32))


@pytest.mark.parametrize("cmd", [
    "bash -c 'echo it'\\''s done; gh pr view 1'",
    "bash -c $'gh pr view 1'",
    "bash -c 'echo 1;'\"gh pr view 1\"",
    'bash <<< "gh pr view 1"',
    'echo "gh pr view 1" | bash',
    'echo "gh pr view 1" | sh -s',
    "\\gh pr list",
    "$(which gh) pr list",
    'g""h pr list',
    'bash -c "$(echo gh) pr view 1"',
    'CMD="gh pr view 1"; bash -c "$CMD"',
    'git commit -m "mention bash -c gh in the message"',
    'git commit -m \'use bash -c "gh pr view" here\'',
])
def test_known_limits_and_ordinary_quotes_do_not_route(cmd):
    assert not ghid.targets_gh(cmd), cmd


def test_the_router_docstring_names_the_known_limits():
    text = ghid.__doc__ or ""
    for phrase in (
        "KNOWN LIMITS",
        "ANSI-C",
        "here-string",
        "$(which gh)",
        'g""h',
        "sh -s",
        'CMD="gh pr view 1"',
        "apostrophe idiom",
    ):
        assert phrase in text, phrase


def test_a_deep_nest_and_a_huge_string_answer_inside_the_hook_timeout():
    started = time.perf_counter()
    assert ghid.targets_gh(_nested("eval", 20000))
    ghid.targets_gh('bash -c "' + ("x" * 1_000_000) + '"')
    ghid.targets_gh('bash -c "gh pr')
    ghid.targets_gh("(" * 20000)
    assert time.perf_counter() - started < 15


def _hook(cmd, monkeypatch, self_name):
    import contextlib
    import io
    monkeypatch.setenv("SWARPH_SELF", self_name)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gh_route.run_hook(json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }))
    raw = buf.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


@pytest.mark.parametrize("n", [33, 64, 1000])
@pytest.mark.parametrize("word", ["eval", "watch"])
def test_past_the_cap_is_rewritten_on_a_mapped_cell_and_denied_on_an_unmapped_one(
        tmp_path, monkeypatch, word, n):
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"lab-ovh": "lab-ovh"}), encoding="utf-8")
    monkeypatch.setenv(ghid.MAPPING_ENV, str(mapping))
    cmd = _nested(word, n)
    rc, out = _hook(cmd, monkeypatch, "lab-ovh")
    assert rc == 0
    rewritten = out["hookSpecificOutput"]["updatedInput"]["command"]
    assert rewritten.startswith("export GH_TOKEN=$(gh auth token --user lab-ovh); ")
    assert cmd in rewritten
    rc, out = _hook(cmd, monkeypatch, "meta-muse")
    assert rc == 0
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
