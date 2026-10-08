"""card #1064 — the stuck-work sweep names every open row blocked >=48h on an
unmoving dependency (DM the BLOCKING holder, never the waiter) and every
approved-or-old open PR with no obligation row naming it. One DM per finding
per 24h; one report line per finding; never closes or reassigns (the only
board write is the DM itself, via /messages).

Fail-first: the sweep module does not exist yet, so the import below is the
first red. The fakes speak the real shapes: obligation rows as
_board_obligation_shape emits them, card graphs as board_card_graph emits
them, gh pr list --json rows as gh emits them.
"""
from __future__ import annotations

import json

import pytest

from swarph_cli.commands import sweep

NOW = 1_790_000_000.0  # fixed clock for age math (2026-09-23ish scale)


def _iso(age_s):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - age_s, timezone.utc).isoformat()


def _row(i, holder="waiter", status="open", step="validate", card_id=7,
         taken_days=None, created_days=10, closed_days=None,
         accept="please review", close_evidence=None):
    r = {"id": i, "card_id": card_id, "holder": holder, "status": status,
         "accept": accept, "close_evidence": close_evidence,
         "close_outcome": None, "holder_known": True}
    if step is not None:
        r["step"] = step
    r["created_at"] = _iso(created_days * 86400)
    r["taken_at"] = _iso(taken_days * 86400) if taken_days is not None else None
    r["closed_at"] = _iso(closed_days * 86400) if closed_days is not None else None
    return r


def _graph(needs):
    """needs: {step: [(need_step, row_id, satisfied)]}."""
    steps = []
    for step, lst in needs.items():
        steps.append({"step": step, "needs": [
            {"step": ns, "row_id": rid, "satisfied": sat,
             "state": "closed:pass" if sat else "open"}
            for ns, rid, sat in lst]})
    return {"steps": steps}


class _Net:
    """Fake board+gh transport. Records every call; board writes are visible."""

    def __init__(self, obligations, graphs, prs, peers=("waiter", "blocker", "author")):
        self.obligations = obligations
        self.graphs = graphs
        self.prs = prs
        self.peers = peers
        self.calls = []
        self.posts = []

    def http_get(self, url, token, timeout=10.0):
        self.calls.append(("GET", url))
        if url.endswith("/graph"):
            cid = int(url.split("/board/cards/")[1].split("/graph")[0])
            if cid not in self.graphs:
                return 404, {"detail": "no such card"}
            return 200, self.graphs[cid]
        if "/board/obligations" in url:
            return 200, {"obligations": self.obligations}
        if url.endswith("/peers"):
            return 200, {"peers": [{"name": p} for p in self.peers]}
        raise AssertionError(f"unexpected GET {url}")

    def http_post(self, url, body, token, timeout=10.0):
        self.calls.append(("POST", url))
        self.posts.append(body)
        return 200, {"id": 1, **body}

    def gh_prs(self, repo):
        self.calls.append(("GH", repo))
        return self.prs.get(repo, [])


def _run(net, tmp_path, **kw):
    return sweep.run_sweep(
        gateway="http://gw:8788", token="tok", sender="sweep",
        repos=["o/r"], state_file=str(tmp_path / "state.json"),
        report_file=str(tmp_path / "report.log"),
        now=NOW, http_get=net.http_get, http_post=net.http_post,
        gh_prs=net.gh_prs, **kw)


def _state(tmp_path):
    p = tmp_path / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def test_blocked_row_dm_goes_to_the_blocker_not_the_waiter(tmp_path):
    """Accept (row): waiter #8 waits on build #7, taken 3d ago and unmoving →
    one DM to the BLOCKING holder naming the waiting row and its age."""
    net = _Net(
        obligations=[
            _row(8, holder="waiter", step="validate", taken_days=1),
            _row(7, holder="blocker", step="build", taken_days=3),
        ],
        graphs={7: _graph({"validate": [("build", 7, False)], "build": []})},
        prs={})
    rc, findings = _run(net, tmp_path)
    assert rc == 0
    assert len(findings) == 1
    assert findings[0]["blocker"] == "blocker"
    assert findings[0]["waiter_id"] == 8
    assert len(net.posts) == 1
    dm = net.posts[0]
    assert dm["to_node"] == "blocker"
    assert dm["from_node"] == "sweep"
    assert "#8" in dm["content"] and "#7" in dm["content"]
    lines = (tmp_path / "report.log").read_text().splitlines()
    assert len(lines) == 1 and "blocker" in lines[0] and "#8" in lines[0]


def test_recently_moved_need_is_not_reported(tmp_path):
    """A needed row taken 2h ago is moving → silence."""
    net = _Net(
        obligations=[
            _row(8, holder="waiter", step="validate", taken_days=1),
            _row(7, holder="blocker", step="build", taken_days=2 / 24),
        ],
        graphs={7: _graph({"validate": [("build", 7, False)], "build": []})},
        prs={})
    rc, findings = _run(net, tmp_path)
    assert (rc, findings, net.posts) == (0, [], [])
    assert not (tmp_path / "report.log").exists()


def test_satisfied_need_is_not_reported(tmp_path):
    net = _Net(
        obligations=[
            _row(8, holder="waiter", step="validate", taken_days=5),
            _row(7, holder="blocker", step="build", taken_days=6,
                 status="closed", closed_days=5),
        ],
        graphs={7: _graph({"validate": [("build", 7, True)], "build": []})},
        prs={})
    rc, findings = _run(net, tmp_path)
    assert (rc, findings, net.posts) == (0, [], [])


def test_stepless_open_row_is_not_a_waiter(tmp_path):
    net = _Net(
        obligations=[_row(8, holder="waiter", step=None, taken_days=9)],
        graphs={},
        prs={})
    rc, findings = _run(net, tmp_path)
    assert (rc, findings, net.posts) == (0, [], [])


def test_approved_pr_with_no_row_is_reported_to_its_author(tmp_path):
    """Accept (PR): approved PR, no row names it → finding + DM to the author."""
    net = _Net(
        obligations=[_row(8, holder="waiter", step=None, taken_days=1)],
        graphs={},
        prs={"o/r": [{"number": 5, "title": "big change",
                      "createdAt": _iso(21 * 86400),
                      "reviewDecision": "APPROVED",
                      "url": "https://github.com/o/r/pull/5",
                      "author": {"login": "author"}}]})
    rc, findings = _run(net, tmp_path)
    assert rc == 0
    assert len(findings) == 1 and findings[0]["kind"] == "pr"
    assert net.posts[0]["to_node"] == "author"
    assert "o/r#5" in net.posts[0]["content"]


def test_old_unapproved_pr_reported_young_unapproved_not(tmp_path):
    net = _Net(
        obligations=[],
        graphs={},
        prs={"o/r": [
            {"number": 6, "title": "old", "createdAt": _iso(3 * 86400),
             "reviewDecision": None,
             "url": "https://github.com/o/r/pull/6",
             "author": {"login": "author"}},
            {"number": 7, "title": "fresh", "createdAt": _iso(2 * 3600),
             "reviewDecision": None,
             "url": "https://github.com/o/r/pull/7",
             "author": {"login": "author"}},
        ]})
    rc, findings = _run(net, tmp_path)
    assert rc == 0
    assert [f["number"] for f in findings] == [6]
    assert [p["to_node"] for p in net.posts] == ["author"]


def test_pr_named_by_row_url_or_number_is_not_reported(tmp_path):
    pr = {"number": 5, "title": "x", "createdAt": _iso(21 * 86400),
          "reviewDecision": "APPROVED",
          "url": "https://github.com/o/r/pull/5",
          "author": {"login": "author"}}
    rows_url = [_row(8, holder="w", step=None, taken_days=1,
                     accept="review https://github.com/o/r/pull/5 please")]
    rows_num = [_row(8, holder="w", step=None, taken_days=1,
                     accept="please review PR 5")]
    for rows in (rows_url, rows_num):
        net = _Net(obligations=rows, graphs={}, prs={"o/r": [pr]})
        rc, findings = _run(net, tmp_path)
        assert (rc, findings, net.posts) == (0, [], []), rows[0]["accept"]
        (tmp_path / "state.json").unlink(missing_ok=True)


def test_same_finding_not_resent_within_24h(tmp_path):
    """Accept (dedup): a finding DM-sent 2h ago is silent; at 25h it refires."""
    net = _Net(
        obligations=[
            _row(8, holder="waiter", step="validate", taken_days=1),
            _row(7, holder="blocker", step="build", taken_days=3),
        ],
        graphs={7: _graph({"validate": [("build", 7, False)], "build": []})},
        prs={})
    rc, findings = _run(net, tmp_path)
    assert rc == 0 and len(findings) == 1
    assert findings[0]["kind"] == "row"
    assert (findings[0]["blocker"], findings[0]["waiter_id"],
            findings[0]["need"], findings[0]["need_id"],
            findings[0]["age_days"]) == ("blocker", 8, "build", 7, 3)
    assert len(net.posts) == 1
    # second run inside the window: silence, and no second report line
    rc, findings = _run(net, tmp_path)
    assert (rc, findings, net.posts[1:]) == (0, [], [])
    assert len((tmp_path / "report.log").read_text().splitlines()) == 1
    # rewind the clock in state past the window: refires
    st = _state(tmp_path)
    key = next(iter(st))
    from datetime import datetime, timezone
    st[key] = datetime.fromtimestamp(
        NOW - 25 * 3600, timezone.utc).isoformat()
    (tmp_path / "state.json").write_text(json.dumps(st))
    rc, findings = _run(net, tmp_path)
    assert rc == 0 and len(findings) == 1 and len(net.posts) == 2


def test_nothing_is_written_to_the_board_except_dms(tmp_path):
    """Accept (no-write): across a mixed run, the only non-GET is POST
    /messages. No card, obligation, or graph writes."""
    net = _Net(
        obligations=[
            _row(8, holder="waiter", step="validate", taken_days=1),
            _row(7, holder="blocker", step="build", taken_days=3),
        ],
        graphs={7: _graph({"validate": [("build", 7, False)], "build": []})},
        prs={"o/r": [{"number": 5, "title": "x",
                      "createdAt": _iso(21 * 86400),
                      "reviewDecision": "APPROVED",
                      "url": "https://github.com/o/r/pull/5",
                      "author": {"login": "author"}}]})
    rc, _ = _run(net, tmp_path)
    assert rc == 0
    for method, url in net.calls:
        if method in ("GET", "GH"):  # board/GH reads are not writes
            continue
        assert method == "POST" and url.endswith("/messages"), (method, url)


def test_unreadable_board_sends_and_writes_nothing(tmp_path):
    class _Down(_Net):
        def http_get(self, url, token, timeout=10.0):
            self.calls.append(("GET", url))
            return 500, {"detail": "down"}

    net = _Down(obligations=[], graphs={}, prs={})
    rc, findings = _run(net, tmp_path)
    assert rc == 1 and findings == [] and net.posts == []
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "report.log").exists()


def test_dry_run_reads_only(tmp_path):
    """Dry mode prints the findings and touches neither state, report, nor
    the DM endpoint — the live-board rehearsal shape."""
    net = _Net(
        obligations=[
            _row(8, holder="waiter", step="validate", taken_days=1),
            _row(7, holder="blocker", step="build", taken_days=3),
        ],
        graphs={7: _graph({"validate": [("build", 7, False)], "build": []})},
        prs={})
    rc, findings = _run(net, tmp_path, dry_run=True)
    assert rc == 0 and len(findings) == 1
    assert net.posts == []
    assert [c for c in net.calls if c[0] == "POST"] == []
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "report.log").exists()


def test_unstaffed_blocking_row_is_report_only(tmp_path):
    """A needed row with no holder names no one to DM (pinging the waiter
    instead is the FAIL) — report line only."""
    net = _Net(
        obligations=[
            _row(8, holder="waiter", step="validate", taken_days=1),
            _row(7, holder=None, step="build", taken_days=3),
        ],
        graphs={7: _graph({"validate": [("build", 7, False)], "build": []})},
        prs={})
    net.obligations[1]["holder_known"] = False
    rc, findings = _run(net, tmp_path)
    assert rc == 0 and len(findings) == 1
    assert findings[0]["blocker"] is None
    assert net.posts == []
    assert len((tmp_path / "report.log").read_text().splitlines()) == 1


def test_unresolvable_pr_author_is_report_only(tmp_path):
    """A gh login with no mesh peer gets no DM (the gateway would take any
    string; the CLI must not invent a recipient) but still a report line."""
    net = _Net(
        obligations=[],
        graphs={},
        prs={"o/r": [{"number": 5, "title": "x",
                      "createdAt": _iso(21 * 86400),
                      "reviewDecision": "APPROVED",
                      "url": "https://github.com/o/r/pull/5",
                      "author": {"login": "stranger"}}]},
        peers=("waiter", "blocker", "author"))
    rc, findings = _run(net, tmp_path)
    assert rc == 0 and len(findings) == 1
    assert findings[0]["blocker"] == "stranger"
    assert net.posts == []
    assert len((tmp_path / "report.log").read_text().splitlines()) == 1
