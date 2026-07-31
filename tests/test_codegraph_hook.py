"""Card #194 — the codegraph-on-grep hook for peers.

Every trigger case below is a REAL command that either fired correctly or
MISFIRED on lab-ovh while the bash prototype ran. They are regressions, not
hypotheticals.
"""
import json

import pytest

from swarph_cli.commands import codegraph_hook as ch


# ── trigger: fires on a code search ───────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "grep -n 'def _board_grant' -A 18 server.py",
    "grep -rn 'links' src/swarph_cli/commands/board.py",
    "rg --include='*.py' 'structural_query'",
    "cat x | grep -n 'def foo' bar.py",
])
def test_fires_on_a_code_search(cmd):
    assert ch._looks_like_code_search(cmd), cmd


# ── trigger: the three guards, each from a live misfire ───────────────────

def test_does_NOT_fire_when_grep_is_merely_MENTIONED():
    """MISFIRE 1: matching `grep` after arbitrary whitespace fired on any command
    whose text merely contains the word — including a heredoc body, where it then
    extracted the heredoc delimiter as the search term."""
    assert not ch._looks_like_code_search("echo 'use grep for src/x.py instead'")


def test_does_NOT_fire_on_a_heredoc():
    """MISFIRE 2: a heredoc means most of the command is DATA, not shell."""
    cmd = "python3 - <<'PY'\nimport re\ngrep = 1  # src/x.py\nPY"
    assert not ch._looks_like_code_search(cmd)


def test_does_NOT_fire_on_a_config_or_log_search():
    """grep is genuinely RIGHT here — the codegraph indexes symbols, not logs.
    Firing would be noise, and noise is how a supplement becomes ignored."""
    assert not ch._looks_like_code_search("grep -n 'ExecStart' /etc/systemd/system/x.service")
    assert not ch._looks_like_code_search("journalctl -u x | grep -i error")


# ── term extraction ───────────────────────────────────────────────────────

def test_extracts_the_term_ADJACENT_to_grep_not_the_first_quoted_string():
    """MISFIRE 3: `gh pr close --comment "...'dead'..." && grep foo bar.py` fired a
    query for 'dead', lifted out of unrelated prose. The scan must start AT grep."""
    cmd = "gh pr close --comment \"the 'dead' branch\" && grep -n 'structural_query' src/x.py"
    assert ch.extract_term(cmd) == "structural_query"


def test_skips_flags_to_reach_the_pattern():
    assert ch.extract_term("grep -rn --include=*.py 'def _board_grant' src/") == "def _board_grant"


# ── the loud-failure contract ─────────────────────────────────────────────

def test_an_unavailable_backend_is_NOT_rendered_as_no_matches():
    """>>> THE LOAD-BEARING BEHAVIOUR. <<< A structural query that cannot run must
    never look like a real negative — that collapse is the (gbrain unreachable)
    incident and card #200's drain, and teaching an agent to read it as 'nothing
    found' is worse than not running at all."""
    out = ch.render("foo", {"error": "HTTP 503: codegraph proxy disabled"})
    assert "UNAVAILABLE" in out
    assert "NOT 'no matches'" in out
    assert "no structural matches" not in out


def test_a_REAL_negative_says_the_index_answered():
    """The other half of the pair: an empty result from a WORKING index must be
    distinguishable from the failure above, or the distinction is decorative."""
    out = ch.render("foo", {"results": [], "freshness": [{"index_age_hours": 2.0}]})
    assert "REAL negative" in out
    assert "UNAVAILABLE" not in out


def test_the_two_render_DIFFERENTLY():
    """Non-vacuity pair — if these ever converge, the hook is lying again."""
    assert ch.render("t", {"error": "boom"}) != ch.render("t", {"results": []})


def test_staleness_is_surfaced_not_swallowed():
    """A stale index yields correct symbols at WRONG line numbers — silently, which
    is the card #193 failure. So the age rides along with every answer."""
    out = ch.render("foo", {"results": [], "freshness": [
        {"index_age_hours": 99.0, "stale": True}]})
    assert "STALE" in out and "99.0h" in out


# ── the entry point never fails a turn ────────────────────────────────────

def test_non_matching_command_emits_nothing_and_exits_0(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(
        {"tool_input": {"command": "ls -la"}})))
    assert ch.run_codegraph_hook([]) == 0
    assert capsys.readouterr().out == ""


def test_missing_identity_is_reported_not_silent(monkeypatch, capsys):
    """No cell identity means the graph was NEVER ASKED. Saying nothing would let
    the agent believe grep's answer was corroborated."""
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(
        {"tool_input": {"command": "grep -n 'def foo' src/x.py"}})))
    assert ch.run_codegraph_hook([]) == 0
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "never asked" in out


def test_output_is_the_documented_hook_json_shape(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(
        {"tool_input": {"command": "grep -n 'def foo' src/x.py"}})))
    monkeypatch.setenv("SWARPH_SELF", "nobody-at-all")
    ch.run_codegraph_hook([])
    d = json.loads(capsys.readouterr().out)
    assert d["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "additionalContext" in d["hookSpecificOutput"]


class _Stdin:
    def __init__(self, s):
        self._s = s

    def read(self):
        return self._s


# ── it is installable ─────────────────────────────────────────────────────

def test_the_hook_is_a_registered_builtin_bundle():
    from swarph_cli.commands import hooks
    b = hooks.resolve_builtin("codegraph-on-grep")
    assert b.script_name == "codegraph-on-grep.sh"
    assert "swarph codegraph-hook" in b.script_body
    assert any(x.event == "PostToolUse" and x.matcher == "Bash" for x in b.bindings)


def test_the_verb_is_registered():
    from swarph_cli.main import _VERB_HANDLERS  # noqa: PLC0415
    assert "codegraph-hook" in _VERB_HANDLERS
