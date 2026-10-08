"""card #1066 — quiet wakes: TAKEN receipts and repo.merged events are logged
but do not wake on their own. Delivered with the next real DM or after a 6h
cap. Per-cell opt-in env (SWARPH_QUIET_WAKES), default off — unset behaviour
is byte-identical to today.

Two surfaces share the classifier (dm_notify_filter.is_quiet_dm):
the streaming pipe (holds in memory, flushes on carry/cap/EOF) and the
channel poll (holds durably in channel_cursor.json, survives restarts).

Fail-first: is_quiet_dm does not exist, so the classifier tests are the
first red; the hold tests go red on today's print-everything paths.
"""
from __future__ import annotations

import io
import json
import queue
import threading
import time

import pytest

from swarph_cli.scripts import dm_notify_filter as dmf
from swarph_cli import channel as ch

QUIET_VAR = "SWARPH_QUIET_WAKES"

TAKEN = {"id": 11, "from_node": "lab-ovh", "to_node": "meta-muse",
         "kind": "answer",
         "content": "OBLIGATION #1362 TAKEN by meta-muse at 2026-10-08T16:10:08 "
                    "(build on card 432)."}
CLOSED = {"id": 12, "from_node": "lab-ovh", "to_node": "meta-muse",
          "kind": "answer",
          "content": "OBLIGATION #1362 CLOSED outcome=pass evidence: done."}
QUESTION = {"id": 13, "from_node": "lab-ovh", "to_node": "meta-muse",
            "kind": "question", "content": "please review PR #1"}
MERGED = {"id": 14, "from_node": "lab-ovh", "to_node": "meta-muse",
          "kind": "fyi",
          "content": json.dumps({"swarph_event": {"event": "repo.merged",
                                                  "repo": "o/r", "pr": 5}})}


def _line(d):
    return json.dumps({"dm": d})


class _Block:
    """An infinite stdin: the test decides when lines (and EOF) arrive."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()

    def __iter__(self):
        while True:
            line = self.q.get()
            if line is None:
                return
            yield line


def _run_stream(lines, *, env_on=True, monkeypatch, **kw):
    """run_filter in a worker over a blocking stream; caller drives lines."""
    if env_on:
        monkeypatch.setenv(QUIET_VAR, "1")
    else:
        monkeypatch.delenv(QUIET_VAR, raising=False)
    stream = _Block()
    out = io.StringIO()
    box = {}
    t = threading.Thread(
        target=lambda: box.setdefault(
            "rc", dmf.run_filter(stream, out, idle_seconds=30, **kw)),
        daemon=True)
    t.start()
    return stream, out, box, t


# ── classifier ───────────────────────────────────────────────────

def test_taken_receipt_is_quiet_but_closed_and_questions_are_not():
    assert dmf.is_quiet_dm(TAKEN) is True
    assert dmf.is_quiet_dm(CLOSED) is False
    assert dmf.is_quiet_dm(QUESTION) is False


def test_repo_merged_event_is_quiet_but_plain_fyi_is_not():
    assert dmf.is_quiet_dm(MERGED) is True
    assert dmf.is_quiet_dm({**MERGED, "content": "just a note"}) is False
    assert dmf.is_quiet_dm({**MERGED, "content": "{not json"}) is False


def test_taken_pattern_in_the_wrong_kind_is_not_quiet():
    assert dmf.is_quiet_dm({**TAKEN, "kind": "question"}) is False


# ── streaming pipe ───────────────────────────────────────────────

def test_taken_alone_fires_no_wake(monkeypatch):
    stream, out, box, t = _run_stream(None, monkeypatch=monkeypatch)
    stream.q.put(_line(TAKEN))
    time.sleep(0.3)
    assert out.getvalue() == "", "a lone TAKEN must print nothing"
    stream.q.put(None)
    t.join(timeout=5)
    assert box["rc"] == 1  # EOF still ends deaf, as today


def test_closed_after_taken_carries_both_in_order(monkeypatch):
    stream, out, box, t = _run_stream(None, monkeypatch=monkeypatch)
    stream.q.put(_line(TAKEN))
    time.sleep(0.2)
    assert out.getvalue() == ""
    stream.q.put(_line(CLOSED))
    time.sleep(0.3)
    lines = out.getvalue().splitlines()
    assert len(lines) == 2, lines
    assert "id=11" in lines[0] and "id=12" in lines[1]
    stream.q.put(None)
    t.join(timeout=5)


def test_lone_quiet_is_delivered_once_the_cap_passes(monkeypatch):
    stream, out, box, t = _run_stream(
        None, monkeypatch=monkeypatch, quiet_cap_s=0.05)
    stream.q.put(_line(TAKEN))
    deadline = time.time() + 5
    while "id=11" not in out.getvalue() and time.time() < deadline:
        time.sleep(0.05)
    assert "id=11" in out.getvalue(), "cap must flush with no new traffic"
    stream.q.put(None)
    t.join(timeout=5)


def test_eof_flushes_held_so_a_dying_watch_loses_nothing(monkeypatch):
    out = io.StringIO()
    monkeypatch.setenv(QUIET_VAR, "1")
    rc = dmf.run_filter(io.StringIO(_line(TAKEN) + "\n"), out,
                        idle_seconds=30)
    assert rc == 1
    assert "id=11" in out.getvalue()


def test_env_unset_prints_taken_like_today(monkeypatch):
    """Accept (default-off): byte-identical — the TAKEN prints immediately."""
    out = io.StringIO()
    monkeypatch.delenv(QUIET_VAR, raising=False)
    rc = dmf.run_filter(io.StringIO(_line(TAKEN) + "\n"), out,
                        idle_seconds=30, max_lines=1)
    assert rc == 0
    assert "id=11" in out.getvalue()


def test_once_holds_quiet_for_the_first_real_dm(monkeypatch, tmp_path):
    """--once with quiet on: TAKEN lines wait; the CLOSED prints backlog +
    itself and returns 0."""
    monkeypatch.setenv(QUIET_VAR, "1")
    inbox = tmp_path / "inbox.log"
    inbox.write_text("", encoding="utf-8")
    out = io.StringIO()
    box = {}

    def _run():
        box["rc"] = dmf.run_once(str(inbox), out, timeout=5.0)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.2)
    with inbox.open("a", encoding="utf-8") as fp:
        fp.write(_line(TAKEN) + "\n")
        fp.write(_line(CLOSED) + "\n")
    t.join(timeout=10)
    assert box["rc"] == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 2 and "id=11" in lines[0] and "id=12" in lines[1]


def test_once_with_only_quiet_returns_silently(monkeypatch, tmp_path):
    """A quiet-only window is not a wake: return 1, print nothing (the lines
    persist in inbox.log for the streaming filter)."""
    monkeypatch.setenv(QUIET_VAR, "1")
    inbox = tmp_path / "inbox.log"
    inbox.write_text(_line(TAKEN) + "\n", encoding="utf-8")
    out = io.StringIO()
    assert dmf.run_once(str(inbox), out, timeout=0.3) == 1
    assert out.getvalue() == ""


# ── channel poll ─────────────────────────────────────────────────

def _inbox(tmp_path, rows):
    p = tmp_path / "inbox.log"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                 encoding="utf-8")
    return p


def _chan(tmp_path, cell="meta-muse"):
    side = tmp_path / cell
    return ch.Channel(cell, side / "inbox.log",
                      side / "channel_cursor.json",
                      side / "channel_heartbeat.json")


T0 = 1791479100.0  # 2026-10-08 17:05Z, after the rows' created_at


def _row(d, cell="meta-muse"):
    return {**d, "to_node": cell,
            "created_at": "2026-10-08T17:00:00+00:00"}


def _armed(tmp_path):
    """First poll on an empty (but present) inbox: arms the cursor."""
    chan = _chan(tmp_path)
    (tmp_path / "meta-muse").mkdir(parents=True, exist_ok=True)
    (tmp_path / "meta-muse" / "inbox.log").write_text("", encoding="utf-8")
    assert len(chan.poll(now=T0 - 100)) == 1  # the armed note
    return chan


def _cursor(tmp_path):
    return json.loads((tmp_path / "meta-muse" / "channel_cursor.json")
                      .read_text(encoding="utf-8"))


def test_channel_holds_quiet_taken_without_pushing(monkeypatch, tmp_path):
    monkeypatch.setenv(QUIET_VAR, "1")
    chan = _armed(tmp_path)
    _inbox(tmp_path / "meta-muse", [_row(TAKEN)])
    assert chan.poll(now=T0) == []
    cur = _cursor(tmp_path)
    assert cur["last_pushed_id"] is None
    assert cur["held_quiet"], "the hold must be durable, not memory-only"


def test_channel_next_real_dm_carries_held(monkeypatch, tmp_path):
    monkeypatch.setenv(QUIET_VAR, "1")
    box = tmp_path / "meta-muse"
    chan = _armed(tmp_path)
    _inbox(box, [_row(TAKEN)])
    assert chan.poll(now=T0) == []
    with (box / "inbox.log").open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(_row(CLOSED)) + "\n")
    notes = chan.poll(now=T0 + 1)
    assert len(notes) == 2, notes
    assert '"id": "11"' in json.dumps(notes[0])
    assert '"id": "12"' in json.dumps(notes[1])


def test_channel_cap_flushes_held_with_no_new_traffic(monkeypatch, tmp_path):
    monkeypatch.setenv(QUIET_VAR, "1")
    chan = _armed(tmp_path)
    _inbox(tmp_path / "meta-muse", [_row(TAKEN)])
    assert chan.poll(now=T0) == []
    notes = chan.poll(now=T0 + 6 * 3600 + 1)
    assert len(notes) == 1
    assert '"id": "11"' in json.dumps(notes[0])


def test_channel_hold_survives_restart_without_dup_or_loss(
        monkeypatch, tmp_path):
    """The cursor file carries the hold: a restarted poll neither loses the
    TAKEN nor pushes it twice."""
    monkeypatch.setenv(QUIET_VAR, "1")
    box = tmp_path / "meta-muse"
    _armed(tmp_path)
    _inbox(box, [_row(TAKEN)])
    assert _chan(tmp_path).poll(now=T0) == []
    assert _chan(tmp_path).poll(now=T0 + 1) == []
    with (box / "inbox.log").open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(_row(QUESTION)) + "\n")
    notes = _chan(tmp_path).poll(now=T0 + 2)
    ids = [n["params"]["meta"]["id"] for n in notes
           if n.get("params", {}).get("meta", {}).get("id")]
    assert ids == ["11", "13"]


def test_channel_held_rows_do_not_trip_the_stall_alarm(
        monkeypatch, tmp_path):
    monkeypatch.setenv(QUIET_VAR, "1")
    chan = _armed(tmp_path)
    _inbox(tmp_path / "meta-muse", [_row(TAKEN)])
    assert chan.poll(now=T0) == []
    assert chan.poll(now=T0 + 121) == []
    assert chan.stall_since is None, (
        "held-but-unpushed rows are covered, not stalled")


def test_channel_env_unset_pushes_taken_immediately(
        monkeypatch, tmp_path):
    """Accept (default-off): byte-identical — no hold, no cursor field."""
    monkeypatch.delenv(QUIET_VAR, raising=False)
    chan = _armed(tmp_path)
    _inbox(tmp_path / "meta-muse", [_row(TAKEN)])
    notes = chan.poll(now=T0)
    assert len(notes) == 1
    cur = _cursor(tmp_path)
    assert cur["last_pushed_id"] == 11
    assert "held_quiet" not in cur
