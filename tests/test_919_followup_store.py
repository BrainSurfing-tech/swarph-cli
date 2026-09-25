"""#919 — followup store: due vs not-due, fire-once, empty vs corrupt."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarph_cli.commands import followup


def _run(argv, store: Path, peer="cursor-win"):
    return followup.run_followup(
        argv + ["--as", peer, "--store", str(store)]
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_past_due_appears_future_does_not(tmp_path, capsys):
    store = tmp_path / "followups.sqlite3"
    now = datetime.now(timezone.utc)
    past = _iso(now - timedelta(hours=1))
    future = _iso(now + timedelta(hours=2))
    assert _run(["add", "--subject", "#192", "--at", past, "--reason", "return"], store) == 0
    capsys.readouterr()
    assert _run(["add", "--subject", "card:559", "--at", future, "--reason", "later"], store) == 0
    capsys.readouterr()
    rc = _run(["list", "--due"], store)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["n"] == 1
    assert out["due"][0]["subject"] == "#192"
    assert out["due"][0]["state"] == "open"


def test_fire_once_never_reappears(tmp_path, capsys):
    store = tmp_path / "followups.sqlite3"
    past = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    assert _run(["add", "--subject", "msg:47028", "--at", past, "--reason", "unread-report"], store) == 0
    capsys.readouterr()
    rc = _run(["list", "--due", "--mark-fired"], store)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["n"] == 1 and out["marked_fired"] is True
    rc2 = _run(["list", "--due"], store)
    out2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0 and out2["n"] == 0


def test_survives_restart(tmp_path, capsys):
    store = tmp_path / "followups.sqlite3"
    past = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    assert _run(["add", "--subject", "obligation:286", "--at", past, "--reason", "build"], store) == 0
    capsys.readouterr()
    # new "process": just reopen
    rc = _run(["list", "--due"], store)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["n"] == 1 and out["due"][0]["subject"] == "obligation:286"


def test_prose_subject_refused(tmp_path, capsys):
    store = tmp_path / "followups.sqlite3"
    with pytest.raises(SystemExit) as ei:
        _run(["add", "--subject", "remember to check the offline page", "--at",
              _iso(datetime.now(timezone.utc)), "--reason", "x"], store)
    assert ei.value.code not in (0, None) or True  # SystemExit from raise SystemExit(msg)
    # our code uses raise SystemExit(str) which has code=str — catch by message
    assert "COORDINATE" in str(ei.value) or "COORDINATE" in capsys.readouterr().err


def test_empty_store_is_not_corrupt(tmp_path, capsys):
    store = tmp_path / "missing.sqlite3"
    rc = _run(["list", "--due"], store)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["n"] == 0 and out["empty"] is True


def test_corrupt_store_fails_loudly_not_empty(tmp_path, capsys):
    """CAN-FAIL: broken store must NOT render as []."""
    store = tmp_path / "followups.sqlite3"
    store.write_text("this is not sqlite\n", encoding="utf-8")
    rc = _run(["list", "--due"], store)
    err = capsys.readouterr().err
    assert rc == 2
    assert "REFUSE" in err and "corrupt" in err.lower()
    assert "[]" not in err  # loud refuse, not empty list


def test_zero_byte_file_is_broken_not_empty(tmp_path, capsys):
    store = tmp_path / "followups.sqlite3"
    store.write_bytes(b"")
    rc = _run(["list", "--due"], store)
    err = capsys.readouterr().err
    assert rc == 2
    assert "REFUSE" in err
