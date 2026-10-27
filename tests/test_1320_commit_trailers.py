"""Card #1320 — the hook adds the trailers, and the check has three cases."""
import os
import subprocess
import sys
from pathlib import Path

from swarph_cli.commit_trailers import append_commit_trailers

ROOT = Path(__file__).resolve().parents[1]


def test_the_hook_adds_card_and_cell():
    out = append_commit_trailers("do the work\n", "1320", "friendly-coder")
    assert "Swarph-Card: #1320" in out
    assert "Swarph-Cell: friendly-coder" in out


def test_a_second_pass_does_not_stack_trailers():
    once = append_commit_trailers("do the work\n", "1320", "friendly-coder")
    twice = append_commit_trailers(once, "1320", "friendly-coder")
    assert twice.count("Swarph-Card:") == 1
    assert twice.count("Swarph-Cell:") == 1


def test_prepare_commit_msg_writes_the_file(tmp_path, monkeypatch):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("do the work\n", encoding="utf-8")
    monkeypatch.setenv("SWARPH_CARD", "1320")
    monkeypatch.setenv("SWARPH_CELL", "friendly-coder")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "prepare-commit-msg-swarph.py"), str(msg)],
        capture_output=True, text=True, check=False, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    text = msg.read_text(encoding="utf-8")
    assert "Swarph-Card: #1320" in text
    assert "Swarph-Cell: friendly-coder" in text


def test_the_three_trailer_cases():
    sys.path.insert(0, str(ROOT))
    from tools.check_card_trailers import evaluate

    def holder_of(card):
        return "friendly-coder" if card == 1320 else None

    ok, reason = evaluate(["nope\n"], holder_of)
    assert ok is False and reason == "names no card"
    ok, reason = evaluate(
        ["Swarph-Card: #1320\nSwarph-Cell: someone-else\n"], holder_of)
    assert ok is False and "someone-else" in reason
    ok, _reason = evaluate(
        ["Swarph-Card: #1320\nSwarph-Cell: friendly-coder\n"], holder_of)
    assert ok is True
