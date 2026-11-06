"""card #1478 — stuck-row DMs name the waiter, the step, and the age.

run_triage.dm_sender used to hand sweep._dm_content a finding whose
waiter, waiter_step, and age_days were None, so a blocker read
"row #N (None, None, card #C) ... unchanged Noned". Rows in this file
are built in the test, not loaded from a fixture.
"""
from __future__ import annotations

from datetime import datetime, timezone

from swarph_cli.commands import triage as triage_cmd
from swarph_cli.triage import backend as backend
from swarph_cli.triage import router as router

NOW = 1_790_000_000.0
HOLDER = "holder-rowan"
BLOCKER = "blocker-peer"
STEP = "validate"


def _iso(age_s):
    return datetime.fromtimestamp(NOW - age_s, timezone.utc).isoformat()


def _http(graph_steps):
    posts = []

    def http_get(url, token):
        if url.endswith("/board/cards/7/graph"):
            return 200, {"steps": graph_steps}
        if url.endswith("/board/cards/8"):
            return 200, {"id": 8, "due_at": None}
        raise AssertionError(f"unexpected GET {url}")

    def http_post(url, body, token):
        posts.append((url, body))
        return 200, {"id": 1, **body}

    return http_get, http_post, posts


def _run(tmp_path, waiter, dep, graph_steps):
    http_get, http_post, posts = _http(graph_steps)
    rows = [waiter, dep]
    rc, summary = triage_cmd.run_triage(
        gateway="http://gw:9", token="tok", sender="triage",
        owners_map=router.load_owners(triage_cmd._OWNERS_DEFAULT),
        state_file=str(tmp_path / "state.json"),
        store_backend=backend.MemoryBackend(),
        peers_fetcher=None,
        board_fetcher=lambda: (rows, {r["id"]: r for r in rows}, {BLOCKER}),
        http_get=http_get, http_post=http_post, now=NOW)
    assert rc == 0 and summary["dm_sent"] == 1
    assert len(posts) == 1 and posts[0][0].endswith("/messages")
    return posts[0][1]["content"]


def _waiter(step):
    row = {
        "id": 8, "status": "open", "holder": HOLDER, "card_id": 7,
        "taken_at": _iso(86400), "created_at": _iso(10 * 86400),
        "holder_known": True,
    }
    if step is not None:
        row["step"] = step
    return row


def _dep():
    """Open need whose last change (_row_last_change) is taken_at, 3 days ago."""
    return {
        "id": 9, "status": "open", "holder": BLOCKER, "card_id": 8,
        "holder_known": True,
        "taken_at": _iso(3 * 86400),
        "created_at": _iso(10 * 86400),
    }


def test_stuck_dm_names_holder_step_and_age(tmp_path):
    """Known waiter, dependency last changed 3 days ago: holder, step, 3d, no None."""
    content = _run(
        tmp_path, _waiter(STEP), _dep(),
        [{"step": STEP, "needs": [
            {"step": "build", "row_id": 9, "satisfied": False}]}])
    assert HOLDER in content
    assert STEP in content
    assert "3d" in content
    assert "None" not in content


def test_waiter_with_no_step_prints_dash_not_none(tmp_path):
    """A waiter with no step prints a dash in the DM, not the string None."""
    content = _run(
        tmp_path, _waiter(None), _dep(),
        [{"step": None, "needs": [
            {"step": "build", "row_id": 9, "satisfied": False}]}])
    assert f"({HOLDER}, -, card #7)" in content
    assert "None" not in content
