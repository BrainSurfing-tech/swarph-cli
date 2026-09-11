"""#825 — UserPromptSubmit trigger + audit counterfactual + Bash shred/floor.

Commander redirect 2026-09-11: PostToolUse can only annotate; UserPromptSubmit
steers. Can-fail trap unchanged: negatives are the test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.commands import codegraph_hook as ch


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


def _home(monkeypatch, tmp_path, with_token=True):
    home = tmp_path / "home"
    if with_token:
        (home / ".config" / "swarph").mkdir(parents=True)
        (home / ".config" / "swarph" / "cursor-win.peer_token").write_text(
            "tok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")
    return home


@pytest.mark.parametrize("cmd", _SHREDDED)
def test_shredded_bash_patterns_emit_nothing(cmd, monkeypatch, capsys, tmp_path):
    _home(monkeypatch, tmp_path)
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("gateway must not be asked for shredded terms")

    monkeypatch.setattr(ch, "query_gateway", _boom)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": cmd},
    })))
    assert ch.run_codegraph_hook([]) == 0
    assert capsys.readouterr().out == ""
    assert called["n"] == 0


def test_good_bash_symbol_still_emits(monkeypatch, capsys, tmp_path):
    _home(monkeypatch, tmp_path)
    env = {"results": [
        {"repo": "lab-orchestrator", "file_path": "a.py", "start_line": 10,
         "kind": "function", "name": "_firejail_argv", "callers": 9},
    ], "freshness": [{"index_age_hours": 1.0}]}
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": _GOOD},
    })))
    assert ch.run_codegraph_hook([]) == 0
    out = capsys.readouterr().out
    assert "_firejail_argv" in out and "callers=9" in out


def test_prompt_without_coding_keywords_emits_nothing(monkeypatch, capsys, tmp_path):
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not query")))
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s1",
        "prompt": "what is the weather in Paris today?",
    })))
    assert ch.run_codegraph_hook([]) == 0
    assert capsys.readouterr().out == ""
    audit = Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-audit.jsonl"
    row = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert row["skipped"] is True
    assert row["skip_reason"] == "no_coding_keywords"


def test_prompt_with_symbol_query_emits_and_registers_pending(
        monkeypatch, capsys, tmp_path):
    home = _home(monkeypatch, tmp_path)
    env = {"results": [
        {"repo": "r", "file_path": "a.py", "start_line": 1,
         "kind": "function", "name": "_firejail_argv", "callers": 9},
    ], "freshness": []}
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s1",
        "prompt": "who calls _firejail_argv in the lab orchestrator?",
    })))
    assert ch.run_codegraph_hook([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "_firejail_argv" in out["hookSpecificOutput"]["additionalContext"]
    pending_path = Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-pending.jsonl"
    opens = [json.loads(x) for x in pending_path.read_text(encoding="utf-8").splitlines()
             if x.strip() and json.loads(x).get("kind") == "open"]
    assert len(opens) == 1


def test_counterfactual_grep_closes_pending(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    env = {"results": [
        {"repo": "r", "file_path": "a.py", "start_line": 1,
         "kind": "function", "name": "_firejail_argv", "callers": 9},
    ], "freshness": []}
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s1",
        "prompt": "where is _firejail_argv defined?",
    })))
    ch.run_codegraph_hook([])
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "tool_input": {"command": "grep -n _firejail_argv src/x.py"},
    })))
    ch.run_codegraph_hook([])
    lines = [
        json.loads(x) for x in
        (Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-audit.jsonl")
        .read_text(encoding="utf-8").splitlines() if x.strip()
    ]
    outcomes = [r for r in lines if r.get("kind") == "outcome"]
    assert outcomes and outcomes[0]["subsequent"] == "grep"
    assert outcomes[0]["subsequent_window"] == "turn"
    assert ch._open_pendings("cursor-win") == []


def test_stop_marks_neither(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    env = {"results": [
        {"repo": "r", "file_path": "a.py", "start_line": 1,
         "kind": "function", "name": "_firejail_argv", "callers": 9},
    ], "freshness": []}
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "s1",
        "prompt": "who calls _firejail_argv?",
    })))
    ch.run_codegraph_hook([])
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "Stop",
        "session_id": "s1",
    })))
    ch.run_codegraph_hook([])
    lines = [
        json.loads(x) for x in
        (Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-audit.jsonl")
        .read_text(encoding="utf-8").splitlines() if x.strip()
    ]
    assert any(r.get("subsequent") == "neither" and r.get("subsequent_window") == "turn"
               for r in lines)


def test_fuzzy_prompt_result_suppressed(monkeypatch, capsys, tmp_path):
    _home(monkeypatch, tmp_path)
    env = {"results": [
        {"repo": "swarph-cli", "file_path": "x.py", "start_line": 1,
         "kind": "function", "name": "standard_setup", "callers": 2},
    ], "freshness": []}
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "prompt": "implement EnvironmentConfig for the fleet module",
    })))
    assert ch.run_codegraph_hook([]) == 0
    # Longest term may be EnvironmentConfig — no row contains it → silence
    assert capsys.readouterr().out == ""


def test_term_is_shredded_no_ident_token():
    raw = r"^\s*[0-9]+[-:]\s*[a-z_]+\s*:"
    cleaned = __import__("re").sub(r"[^\w\s.]", " ", raw).strip()
    assert ch.term_is_shredded(raw, cleaned)
    assert not ch.term_is_shredded("_firejail_argv", "_firejail_argv")
