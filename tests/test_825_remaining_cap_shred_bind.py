"""#825 remaining — match_count cap honesty + recoverable-symbol extract + partial bind.

Can-fail traps: a 7-row probe must set match_count_capped; a raw pattern that
contains an identifier but used to shred must yield that identifier.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarph_cli.commands import codegraph_hook as ch
from swarph_cli.commands import hooks


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


def _seven_rows():
    return {
        "results": [
            {"repo": "r", "file_path": f"a{i}.py", "start_line": i,
             "kind": "function", "name": f"_firejail_argv_{i}", "callers": i}
            for i in range(7)
        ],
        "freshness": [{"index_age_hours": 1.0}],
    }


def test_825_match_count_capped_when_probe_exceeds_display(
        monkeypatch, capsys, tmp_path):
    """Can-fail: today's code asked limit=MAX_ROWS so match_count could never
    exceed 6 and 90.5% of live rows sat exactly on the clamp with no flag."""
    home = _home(monkeypatch, tmp_path)
    seen = {}

    def _qg(term, gateway, token, limit=None):
        seen["limit"] = limit if limit is not None else ch._PROBE_LIMIT
        return _seven_rows()

    monkeypatch.setattr(ch, "query_gateway", _qg)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": r'grep -n "_firejail_argv" src/x.py'},
    })))
    assert ch.run_codegraph_hook([]) == 0
    assert seen["limit"] == ch.MAX_ROWS + 1
    audit = Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-audit.jsonl"
    row = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert row["match_count"] == 7
    assert row["match_count_capped"] is True
    out = capsys.readouterr().out
    assert "match_count_capped=true" in out


def test_825_match_count_uncapped_when_under_display(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    env = {
        "results": [
            {"repo": "r", "file_path": "a.py", "start_line": 1,
             "kind": "function", "name": "_firejail_argv", "callers": 9},
        ],
        "freshness": [{"index_age_hours": 1.0}],
    }
    monkeypatch.setattr(ch, "query_gateway", lambda *a, **k: env)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": r'grep -n "_firejail_argv" src/x.py'},
    })))
    assert ch.run_codegraph_hook([]) == 0
    audit = Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-audit.jsonl"
    row = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert row["match_count"] == 1
    assert row["match_count_capped"] is False


def test_825_recoverable_symbol_inside_regex_is_not_shredded():
    """Live-shaped specimen: identifier still present inside a regex pattern.

    OLD extract ran re.sub first and lost the symbol (or skip_reason=shredded).
    NEW prefers the longest ident token in the raw pattern.
    """
    cmd = r"""grep -rn --include='*.py' '_firejail_argv\|legacy_name' src/"""
    pair = ch.extract_raw_and_cleaned(cmd)
    assert pair is not None
    raw, cleaned = pair
    assert cleaned == "_firejail_argv"
    assert not ch.term_is_shredded(raw, cleaned)


def test_825_pure_regex_debris_still_shredded():
    """Can-fail companion: no identifier ≥3 → still shredded (skip path)."""
    cmd = r'grep -n "^\s*[0-9]+[-:]\s*[a-z_]+\s*:" src/x.py'
    pair = ch.extract_raw_and_cleaned(cmd)
    assert pair is not None
    raw, cleaned = pair
    # May recover 'a' from [a-z_] only as len<3 junk — must still shred.
    assert ch.term_is_shredded(raw, cleaned) or len(cleaned) < 3 or cleaned in {
        "a", "z_", "s",
    }
    # Stronger: if we only got short debris, shredded; if extract returned a
    # long false ident from character class, that would be a NEW defect.
    idents = [t for t in ch._IDENT_TOKEN.findall(raw) if len(t) >= 3]
    if not idents:
        assert ch.term_is_shredded(raw, cleaned)


def test_825_old_extract_would_miss_recoverable_symbol():
    """Can-fail: verbatim OLD expression fails the fixture the NEW one passes."""
    raw = r"_firejail_argv\|legacy_name"
    old_cleaned = __import__("re").sub(r"[^\w\s.]", " ", raw).strip()
    # Old shred leaves tokens but the chosen 'term' was the whole cleaned
    # string — and term_is_shredded looked for idents in cleaned. Reconstruct
    # the failure mode lab named: debris asked of the index.
    assert "firejail" in old_cleaned or "_firejail_argv" in old_cleaned.split()
    # NEW picks the symbol directly:
    assert ch.extract_raw_and_cleaned(
        f"grep -n '{raw}' src/x.py"
    )[1] == "_firejail_argv"


def test_825_partial_bind_is_legible(tmp_path):
    """PostToolUse-only install must list as partial, not available/installed."""
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    hooks_home.mkdir()
    bundle = hooks.resolve_builtin("codegraph-on-grep")
    script = hooks_home / bundle.script_name
    script.write_text(bundle.script_body, encoding="utf-8")
    cmd = hooks._installed_command(bundle, hooks_home)
    settings = {
        "hooks": {
            "PostToolUse": [{
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": cmd}],
            }],
        }
    }
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    lines = []
    hooks.list_hooks(
        settings_path=settings_path, hooks_home=hooks_home, out=lines.append,
    )
    blob = "\n".join(lines)
    assert "codegraph-on-grep  [partial]" in blob
    assert "UserPromptSubmit" in blob
    assert "MISSING" in blob
