"""Tests for ``swarph import`` command + verb dispatch."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from swarph_cli.commands.import_session import (
    _detect_format,
    run_import,
    _swarph_native_path,
)
from swarph_cli.main import main as cli_main


def _write_claude_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def claude_session(tmp_path):
    """A small, clean Claude session JSONL fixture."""
    return _write_claude_jsonl(tmp_path / "session.jsonl", [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": "S-test"},
        {"type": "user", "sessionId": "S-test",
         "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "sessionId": "S-test",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "hi"}]}},
        {"type": "user", "sessionId": "S-test",
         "message": {"role": "user", "content": "what's up"}},
        {"type": "assistant", "sessionId": "S-test",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [{"type": "text", "text": "not much"}]}},
    ])


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_detect_format_claude_jsonl(claude_session):
    assert _detect_format(claude_session) == "claude"


def test_detect_format_unknown_extension(tmp_path):
    f = tmp_path / "session.txt"
    f.write_text("hello", encoding="utf-8")
    assert _detect_format(f) is None


def test_detect_format_empty_jsonl(tmp_path):
    f = tmp_path / "empty.jsonl"
    f.write_text("", encoding="utf-8")
    assert _detect_format(f) is None


def test_detect_format_non_claude_jsonl(tmp_path):
    """A JSONL file with a non-Claude record discriminator returns None."""
    f = tmp_path / "other.jsonl"
    f.write_text(json.dumps({"type": "future-tool-record"}) + "\n",
                 encoding="utf-8")
    assert _detect_format(f) is None


# ---------------------------------------------------------------------------
# --report-only mode
# ---------------------------------------------------------------------------


def test_report_only_prints_human_report_to_stdout(claude_session, capsys):
    rc = run_import([str(claude_session), "--report-only"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Claude session report" in captured.out
    assert "user turns:        2" in captured.out
    assert "assistant turns:   2" in captured.out
    assert "Honest framing" in captured.out


def test_report_only_with_json_flag_emits_json(claude_session, capsys):
    rc = run_import([str(claude_session), "--report-only", "--json-report"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["source_format"] == "claude"
    assert payload["user_turns"] == 2
    assert payload["assistant_turns"] == 2
    assert payload["session_id"] == "S-test"
    assert payload["model_seen"] == "claude-opus-4-7"


def test_report_only_does_not_write_session_file(claude_session, capsys, tmp_path, monkeypatch):
    """--report-only must not create ~/.swarph/sessions/<...>.jsonl."""
    swarph_home = tmp_path / "swarph_home"
    monkeypatch.setattr(Path, "home", lambda: swarph_home)
    rc = run_import([str(claude_session), "--report-only"])
    assert rc == 0
    sessions_dir = swarph_home / ".swarph" / "sessions"
    # Either the directory wasn't created, or it's empty
    if sessions_dir.exists():
        assert list(sessions_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Write path — refuse-with-error default + --force + --target-session
# ---------------------------------------------------------------------------


def test_default_write_creates_swarph_native_session(claude_session, capsys, tmp_path, monkeypatch):
    swarph_home = tmp_path / "swarph_home"
    monkeypatch.setattr(Path, "home", lambda: swarph_home)
    rc = run_import([str(claude_session)])
    assert rc == 0
    sessions = list((swarph_home / ".swarph" / "sessions").iterdir())
    assert len(sessions) == 1
    out_path = sessions[0]
    # First line is metadata header
    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5  # 1 header + 4 turns
    header = json.loads(lines[0])
    assert "_meta" in header
    assert header["_meta"]["imported_from"]["format"] == "claude"
    assert header["_meta"]["imported_from"]["session_id"] == "S-test"
    # All turn lines have _meta.source = "imported"
    for line in lines[1:]:
        row = json.loads(line)
        assert row["_meta"]["source"] == "imported"


def test_refuse_with_error_when_target_exists(claude_session, capsys, tmp_path, monkeypatch):
    swarph_home = tmp_path / "swarph_home"
    monkeypatch.setattr(Path, "home", lambda: swarph_home)
    # First import — succeeds
    assert run_import([str(claude_session)]) == 0
    # Second import — should refuse-with-error (no --force, no --target-session)
    rc = run_import([str(claude_session)])
    assert rc == 3
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert "--force" in captured.err
    assert "--target-session" in captured.err


def test_force_flag_overwrites_existing(claude_session, capsys, tmp_path, monkeypatch):
    swarph_home = tmp_path / "swarph_home"
    monkeypatch.setattr(Path, "home", lambda: swarph_home)
    assert run_import([str(claude_session)]) == 0
    # Re-import with --force succeeds
    rc = run_import([str(claude_session), "--force"])
    assert rc == 0


def test_target_session_writes_to_different_file(claude_session, capsys, tmp_path, monkeypatch):
    swarph_home = tmp_path / "swarph_home"
    monkeypatch.setattr(Path, "home", lambda: swarph_home)
    assert run_import([str(claude_session)]) == 0
    # Different --target-session does NOT collide
    rc = run_import([str(claude_session), "--target-session", "alt-name"])
    assert rc == 0
    sessions_dir = swarph_home / ".swarph" / "sessions"
    files = sorted(p.name for p in sessions_dir.iterdir())
    # Both files exist
    assert "alt-name.jsonl" in files
    assert len(files) == 2


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_source_exits_2(capsys, tmp_path):
    rc = run_import([str(tmp_path / "does-not-exist.jsonl"), "--report-only"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "source file not found" in captured.err


def test_unsupported_source_format_exits_2(capsys, tmp_path):
    """A non-claude .jsonl with a recognised-but-unsupported format
    flag fails fast."""
    f = tmp_path / "x.jsonl"
    f.write_text(json.dumps({"type": "future-thing"}) + "\n", encoding="utf-8")
    rc = run_import([str(f), "--report-only"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "could not detect source format" in captured.err


def test_explicit_source_format_override(claude_session, capsys):
    """--source-format claude works even on an ambiguously-named file."""
    rc = run_import([str(claude_session), "--source-format", "claude", "--report-only"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Verb dispatch from main()
# ---------------------------------------------------------------------------


def test_main_dispatches_import_verb(claude_session, capsys, tmp_path, monkeypatch):
    swarph_home = tmp_path / "swarph_home"
    monkeypatch.setattr(Path, "home", lambda: swarph_home)
    rc = cli_main(argv=["import", str(claude_session), "--report-only"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Claude session report" in captured.out


def test_main_one_shot_path_unaffected_by_verb_dispatch():
    """`swarph "hello"` (one-shot) still goes to the one-shot path —
    the verb-dispatch only fires when first arg matches a known verb."""
    # A prompt that isn't a verb keyword should NOT trigger import dispatch.
    # It will fail downstream (no GEMINI_API_KEY in test env) but the
    # routing is what's under test.
    # Use a prompt that doesn't start with "import"
    rc = cli_main(argv=["just a regular prompt", "--provider", "fake"])
    # Will hit UnknownProvider via SwarphCall.adapter, returns 1
    assert rc == 1


def test_subprocess_invocation_routes_import(claude_session, tmp_path):
    """End-to-end: `python -m swarph_cli.main import <path> --report-only`
    via subprocess routes to the import handler and exits 0."""
    result = subprocess.run(
        [sys.executable, "-m", "swarph_cli.main",
         "import", str(claude_session), "--report-only"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "Claude session report" in result.stdout


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_swarph_native_path_uses_target_session_when_given(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from swarph_cli.parsers.claude import ImportReport, ImportResult

    result = ImportResult(
        messages=[],
        report=ImportReport(source_path="/x/foo.jsonl"),
        metadata={"session_id": "S-1"},
    )
    p = _swarph_native_path("custom", result)
    assert p.name == "custom.jsonl"
    assert p.parent == tmp_path / ".swarph" / "sessions"


def test_swarph_native_path_defaults_to_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from swarph_cli.parsers.claude import ImportReport, ImportResult

    result = ImportResult(
        messages=[],
        report=ImportReport(source_path="/x/foo.jsonl"),
        metadata={"session_id": "S-fancy"},
    )
    p = _swarph_native_path(None, result)
    assert p.name == "S-fancy.jsonl"


def test_swarph_native_path_falls_back_to_filename_stem(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from swarph_cli.parsers.claude import ImportReport, ImportResult

    result = ImportResult(
        messages=[],
        report=ImportReport(source_path="/x/no-session-id.jsonl"),
        metadata={},
    )
    p = _swarph_native_path(None, result)
    assert p.name == "no-session-id.jsonl"
