"""#829 — the counterfactual column must not be writable by the wrong session
or the wrong clock, and must not lose open firings to unlocked RMW.
"""
from __future__ import annotations

import json
from pathlib import Path

from swarph_cli.commands import codegraph_hook as ch


def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "swarph").mkdir(parents=True)
    (home / ".config" / "swarph" / "cursor-win.peer_token").write_text("tok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")
    return home


def test_empty_session_id_resolves_NOTHING(monkeypatch, tmp_path):
    """Can-fail: today's bug closed EVERY pending row when session_id == ''."""
    home = _home(monkeypatch, tmp_path)
    ch.register_pending_firing("cursor-win", "f-other", "sess-A")
    ch.register_pending_firing("cursor-win", "f-mine", "sess-B")
    ch.resolve_pending_outcome("cursor-win", "neither", session_id="")
    assert {o["firing_id"] for o in ch._open_pendings("cursor-win")} == {"f-other", "f-mine"}
    audit = Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-audit.jsonl"
    assert not audit.exists()


def test_session_resolve_does_not_touch_other_sessions(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    ch.register_pending_firing("cursor-win", "f-A", "sess-A")
    ch.register_pending_firing("cursor-win", "f-B", "sess-B")
    ch.resolve_pending_outcome("cursor-win", "grep", session_id="sess-A")
    left = {o["firing_id"] for o in ch._open_pendings("cursor-win")}
    assert left == {"f-B"}


def test_pending_is_append_only_jsonl(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    ch.register_pending_firing("cursor-win", "f1", "s1")
    ch.register_pending_firing("cursor-win", "f2", "s1")
    path = Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-pending.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert all(r["kind"] == "open" for r in lines)
    assert len(lines) == 2
    ch.resolve_pending_outcome("cursor-win", "neither", session_id="s1")
    lines2 = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(r["kind"] == "closed" for r in lines2)
    assert ch._open_pendings("cursor-win") == []


def test_outcome_labels_turn_window(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    ch.register_pending_firing("cursor-win", "f1", "s1")
    ch.resolve_pending_outcome("cursor-win", "neither", session_id="s1")
    row = json.loads(
        (Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-audit.jsonl")
        .read_text(encoding="utf-8").splitlines()[0])
    assert row["subsequent_window"] == "turn"
    assert "per-turn" in row["subsequent_means"]


def test_pending_compacts_when_closed_exceed_threshold(monkeypatch, tmp_path):
    """Lab #829 follow-up: closed rows are dead weight; rewrite to opens, never a tail bound."""
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setattr(ch, "_PENDING_CLOSED_COMPACT", 5)
    for i in range(6):
        ch._append_pending("cursor-win", {
            "kind": "closed",
            "firing_id": f"dead-{i}",
            "subsequent": "neither",
            "session_id": "s1",
        })
    ch.register_pending_firing("cursor-win", "still-open", "s2")
    live = ch._open_pendings("cursor-win")
    assert {o["firing_id"] for o in live} == {"still-open"}
    path = Path(home) / "swarph_state" / "cursor-win" / "codegraph-hook-pending.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert all(r["kind"] == "open" for r in lines)
    assert len(lines) == 1
    assert lines[0]["firing_id"] == "still-open"
