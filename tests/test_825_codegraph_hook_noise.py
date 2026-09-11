"""#825 — codegraph-on-grep must not ask the index shredded regexes, and must
not print a FUZZY MATCH block when no returned name contains the query.

CAN-FAIL trap (card): a test that only checks a good query (`_firejail_argv`)
passes on TODAY's code. The defect is what the hook does with BAD queries —
those negatives are the test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.commands import codegraph_hook as ch


# Measured this session (card #825) — patterns that become soup under re.sub
# AND trip the skip guard (no identifier token >=3). Alternation regexes that
# still have real tokens are covered by the relevance-floor test below.
_SHREDDED = [
    r'grep -n "^\s*[0-9]+[-:]\s*[a-z_]+\s*:" src/x.py',
    r'grep -n "^[0-9]+.*[a-z_]+$" foo.py',
]

_GOOD = r'grep -n "_firejail_argv" src/swarph_cli/commands/x.py'


class _Stdin:
    def __init__(self, s):
        self._s = s

    def read(self):
        return self._s


@pytest.mark.parametrize("cmd", _SHREDDED)
def test_shredded_patterns_are_skipped_before_query(cmd, monkeypatch, capsys, tmp_path):
    """Negatives: emit NOTHING — do not query, do not banner."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows Path.home()
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("gateway must not be asked for shredded terms")

    monkeypatch.setattr(ch, "query_gateway", _boom)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(
        {"tool_input": {"command": cmd}})))
    assert ch.run_codegraph_hook([]) == 0
    assert capsys.readouterr().out == ""
    assert called["n"] == 0


def test_good_symbol_query_still_emits_caller_rows(monkeypatch, capsys, tmp_path):
    """Positive half of the can-fail: `_firejail_argv` still surfaces."""
    home = tmp_path / "home"
    (home / ".config" / "swarph").mkdir(parents=True)
    (home / ".config" / "swarph" / "cursor-win.peer_token").write_text("tok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")

    env = {"results": [
        {"repo": "lab-orchestrator", "file_path": "a.py", "start_line": 10,
         "kind": "function", "name": "_firejail_argv", "callers": 9},
    ], "freshness": [{"index_age_hours": 1.0}]}
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(
        {"tool_input": {"command": _GOOD}})))
    assert ch.run_codegraph_hook([]) == 0
    out = capsys.readouterr().out
    assert "_firejail_argv" in out
    assert "callers=9" in out
    assert "FUZZY MATCH" not in out


def test_alternation_regex_suppressed_by_relevance_floor_when_gateway_returns_noise(
        monkeypatch, capsys, tmp_path):
    """`Standard|Exec|Environment` may clear the skip guard (real tokens) but
    the relevance floor must still silence a non-matching result set."""
    home = tmp_path / "home"
    (home / ".config" / "swarph").mkdir(parents=True)
    (home / ".config" / "swarph" / "cursor-win.peer_token").write_text("tok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")

    # Longest token is Environment — no returned name contains it.
    env = {"results": [
        {"repo": "swarph-cli", "file_path": "x.py", "start_line": 1,
         "kind": "function", "name": "standard_setup", "callers": 2},
    ], "freshness": [{"index_age_hours": 1.0}]}
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    cmd = r'grep -E "Standard|Exec|Environment" src/x.py'
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(
        {"tool_input": {"command": cmd}})))
    assert ch.run_codegraph_hook([]) == 0
    assert capsys.readouterr().out == ""


def test_audit_jsonl_records_skip_and_hit(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "swarph").mkdir(parents=True)
    (home / ".config" / "swarph" / "cursor-win.peer_token").write_text("tok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")

    # skip path
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(
        {"tool_input": {"command": _SHREDDED[0]}})))
    ch.run_codegraph_hook([])

    env = {"results": [
        {"repo": "r", "file_path": "a.py", "start_line": 1,
         "kind": "function", "name": "_firejail_argv", "callers": 9},
    ], "freshness": []}
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(
        {"tool_input": {"command": _GOOD}})))
    ch.run_codegraph_hook([])

    audit = Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-audit.jsonl"
    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 2
    assert lines[0]["skipped"] is True
    assert lines[0]["skip_reason"] == "shredded_term"
    assert "raw_command" in lines[0] and "extracted_term" in lines[0]
    assert lines[1]["skipped"] is False
    assert lines[1]["match_count"] == 1
    assert lines[1]["any_name_contains_term"] is True


def test_term_is_shredded_candidate_rule():
    raw = r"^\s*[0-9]+[-:]\s*[a-z_]+\s*:"
    cleaned = re_sub_clean(raw)
    assert ch.term_is_shredded(raw, cleaned)

    assert not ch.term_is_shredded("_firejail_argv", "_firejail_argv")


def re_sub_clean(term: str) -> str:
    import re
    return re.sub(r"[^\w\s.]", " ", term).strip()
