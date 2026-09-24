"""#126 — pending_from must cover the same set as pending, not the 50-entry deque.

The deque bounds RETURNED LINES. Taking distinct senders from it understates who
is waiting: it keeps the NEWEST 50, so the longest-waiting senders fall off first.
"""
from __future__ import annotations

import json
from pathlib import Path

from swarph_cli.commands import mesh, monitor


def _line(msg_id: int, from_node: str) -> str:
    return json.dumps({
        "id": msg_id,
        "from_node": from_node,
        "kind": "question",
        "content": f"m{msg_id}",
    }) + "\n"


def _old_pending_from_from_deque(dms: list) -> list[str]:
    """TODAY'S BUG — senders taken from the capped list only.

    Kept as a named can-fail: this fixture must make the old expression return
    the truncated set while the new helper returns the full N. A test that
    passed on both old and new code is the accept's FAIL branch.
    """
    return sorted({str(d.get("from_node")) for d in dms})


def test_126_pending_from_covers_full_set_not_deque(tmp_path: Path):
    """One chatty peer fills the newest 50; N other senders sit just behind.

    OLD code (deque-only) returns {chatty}. NEW code returns all N+1 names.
    """
    log = tmp_path / "inbox.log"
    early_senders = [f"peer-{i}" for i in range(8)]  # N=8 long-waiting
    chatty = "chatty-peer"
    lines: list[str] = []
    mid = 1
    for s in early_senders:
        lines.append(_line(mid, s))
        mid += 1
    # Newest 50 all from one peer — fills the deque, evicts the eight above.
    for _ in range(50):
        lines.append(_line(mid, chatty))
        mid += 1
    log.write_text("".join(lines), encoding="utf-8")

    dms, skipped, pending_from = mesh._replay_from_inbox_log(
        log, after_id=0, limit=mesh._MONITOR_REPLAY_LIMIT
    )
    assert len(dms) == 50
    assert skipped == 8
    assert all(d["from_node"] == chatty for d in dms)

    old = _old_pending_from_from_deque(dms)
    assert old == [chatty], (
        f"can-fail: today's code must return the truncated set; got {old}"
    )
    expected = sorted(early_senders + [chatty])
    assert pending_from == expected, (
        f"full-set pending_from must be {expected}; got {pending_from}"
    )
    assert len(pending_from) == 9
    assert len(old) < len(pending_from), (
        "can-fail proof: old truncated set must be STRICTLY smaller than new"
    )


def test_126_monitor_collect_uses_full_pending_from(tmp_path: Path, monkeypatch):
    """status path must not re-derive senders from the capped dms list."""
    import argparse

    early = [f"s{i}" for i in range(5)]
    lines = [_line(i + 1, early[i]) for i in range(5)]
    lines += [_line(100 + i, "noisy") for i in range(50)]
    (tmp_path / "inbox.log").write_text("".join(lines), encoding="utf-8")
    (tmp_path / "ledgers.json").write_text(json.dumps({
        "pull": {
            "last_delivered_id": 0,
            "last_delivery_at": 0.0,
            "consecutive_failures": 0,
        }
    }), encoding="utf-8")
    (tmp_path / "cursor.json").write_text(
        json.dumps({"last_msg_id": 149}), encoding="utf-8"
    )
    monkeypatch.setattr(mesh, "pidfile_status", lambda p: ("live_ours", {
        "pid": 1, "self": "cursor-win", "sinks": ["pull"],
    }))

    args = argparse.Namespace(
        self_name="cursor-win",
        state_dir=str(tmp_path),
        gateway="http://example.invalid",
        token_file=None,
        json=False,
        brief=False,
    )
    info = monitor._collect(args)
    row = next(r for r in info["sinks"] if r["name"] == "pull")
    assert row["pending"] == 55
    assert set(row["pending_from"]) == set(early + ["noisy"])
    assert "unread DM" not in row["label"], (
        f"label must not say bare 'unread DM'; got {row['label']!r}"
    )
    assert "ledger" in row["label"].lower() or "newer than" in row["label"].lower()


def test_126_pull_label_does_not_claim_gateway_unread():
    label = mesh.PullSink().pending_label(3)
    assert "unread DM" not in label
    assert "3" in label
