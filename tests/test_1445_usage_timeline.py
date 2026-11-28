"""Card #1445 — `swarph usage timeline`: read-only obligation history.

Recorded JSON only, zero network by construction: the fake transport answers
the two GET shapes the command uses (obligations list, one card) and records
every call, so any write-shaped call would fail the test loudly.

Fixtures are synthetic. Timestamps are UTC ISO with Z.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone

import pytest

from swarph_cli.commands import usage as usage_cmd
from swarph_cli.usage import timeline as tl

AS_OF = "2026-11-28T00:00:00Z"

CARD = {"id": 1765, "project_id": 3, "stage": "build",
        "title": "timeline demo card"}


def _row(i, *, card=1765, step="build", task=None, holder="alice",
         status="open", created=None, taken=None, closed=None, outcome=None):
    r = {"id": i, "card_id": card, "step": step, "holder": holder,
         "status": status, "created_at": created, "taken_at": taken,
         "closed_at": closed, "close_outcome": outcome}
    if task is not None:
        r["task"] = task
    return r


def _reworked_card_rows():
    """Two build attempts on one task, a parallel task, an open row, a
    commander-held row, a close-without-take, a never-taken row, and a row
    with no created_at (sorts last)."""
    return [
        _row(11, step="build", task="task:pr-demo-9", holder="alice",
             status="closed", created="2026-11-20T10:00:00Z",
             taken="2026-11-20T10:05:00Z", closed="2026-11-20T12:05:00Z",
             outcome="pass"),
        _row(12, step="build", task="task:pr-demo-9", holder="alice",
             status="closed", created="2026-11-21T10:00:00Z",
             taken="2026-11-21T10:10:00Z", closed="2026-11-21T11:10:00Z",
             outcome="fail"),
        _row(13, step="build", task="task:pr-demo-10", holder="bob",
             status="closed", created="2026-11-22T09:00:00Z",
             taken="2026-11-22T09:30:00Z", closed="2026-11-22T10:30:00Z",
             outcome="pass"),
        _row(14, step="validate", holder="alice", status="open",
             created="2026-11-27T10:00:00Z", taken="2026-11-27T11:00:00Z"),
        _row(15, step="plan-review", holder="commander", status="closed",
             created="2026-11-25T08:00:00Z", taken="2026-11-25T09:00:00Z",
             closed="2026-11-25T10:30:00Z", outcome="pass"),
        _row(16, step="build", task="task:pr-demo-9", holder="alice",
             status="closed", created="2026-11-26T10:00:00Z",
             taken=None, closed="2026-11-26T12:00:00Z", outcome="fail"),
        _row(17, step="validate", holder="alice", status="open",
             created="2026-11-27T20:00:00Z"),
        _row(18, step="build", task="task:pr-demo-9", holder="alice",
             status="closed", created=None,
             taken="2026-11-27T10:00:00Z", closed="2026-11-27T11:00:00Z",
             outcome="pass"),
    ]


def _peer_day_rows():
    """Day under test: 2026-11-27. Covers the midnight boundaries, every
    role, a missing taken_at, and a row on another card."""
    return [
        _row(21, step="build", holder="friendly-coder", status="open",
             created="2026-11-26T10:00:00Z", taken="2026-11-26T23:59:59Z"),
        _row(22, step="build", holder="friendly-coder", status="open",
             created="2026-11-27T00:00:00Z", taken="2026-11-27T00:00:00Z"),
        _row(23, step="validate", holder="friendly-coder", status="closed",
             created="2026-11-20T09:00:00Z", taken="2026-11-20T10:00:00Z",
             closed="2026-11-27T12:00:00Z", outcome="pass"),
        _row(24, step="build", holder="friendly-coder", status="closed",
             created="2026-11-27T07:00:00Z", taken="2026-11-27T08:00:00Z",
             closed="2026-11-27T09:30:00Z", outcome="pass"),
        _row(25, step="build", holder="friendly-coder", status="open",
             created="2026-11-27T09:00:00Z", taken="2026-11-27T10:00:00Z",
             closed="2026-11-28T00:00:00Z"),
        _row(26, step="validate", holder="friendly-coder", status="closed",
             created="2026-11-27T14:00:00Z", taken=None,
             closed="2026-11-27T15:00:00Z", outcome="fail"),
        _row(27, step="build", holder="friendly-coder", status="open",
             created="2026-11-27T16:00:00Z"),
        _row(28, card=1770, step="build", holder="friendly-coder",
             status="open", created="2026-11-27T10:30:00Z",
             taken="2026-11-27T11:00:00Z"),
    ]


def _harness(monkeypatch, tmp_path, *, obligations, card=CARD):
    """Fake the single GET transport the timeline path uses. Records calls."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    calls = []

    def fake_get(url, token, *, timeout=10.0):
        calls.append(url)
        assert token == "shared-auth"
        if "/board/obligations" in url:
            return 200, {"obligations": obligations, "as_of": AS_OF}
        if "/board/cards/" in url:
            return 200, dict(card)
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(usage_cmd, "_http_get_json", fake_get)
    return calls


def _run(argv, monkeypatch, tmp_path, *, obligations, card=CARD):
    calls = _harness(monkeypatch, tmp_path, obligations=obligations,
                     card=card)
    base = ["timeline", "--as-of", AS_OF, "--as", "meta-muse",
            "--gateway", "http://gw:8788"]
    rc = usage_cmd.run_usage(base + argv)
    return rc, calls


# ── mode A: card history ─────────────────────────────────────────────────

def test_card_rows_sort_chronologically_undated_last(monkeypatch, tmp_path,
                                                    capsys):
    rc, _ = _run(["--card", "1765"], monkeypatch, tmp_path,
                 obligations=_reworked_card_rows())
    assert rc == 0
    out = capsys.readouterr().out
    ids = [int(line.split()[0][1:])
           for line in out.splitlines() if line.startswith("  #")]
    assert ids == [11, 12, 13, 15, 16, 14, 17, 18]


def test_rework_ordinal_groups_by_step_and_task(monkeypatch, tmp_path, capsys):
    rc, _ = _run(["--card", "1765"], monkeypatch, tmp_path,
                 obligations=_reworked_card_rows())
    assert rc == 0
    out = capsys.readouterr().out
    kinds = {}
    for line in out.splitlines():
        if line.startswith("  #"):
            rid = int(line.split()[0][1:])
            rk = [tok for tok in line.split() if tok.startswith("rk#")][0]
            kinds[rid] = rk
    # Same (step, task): original then reworks in creation order.
    assert kinds[11] == "rk#1"
    assert kinds[12] == "rk#2"
    assert kinds[16] == "rk#3"
    assert kinds[18] == "rk#4"
    # Same step but a DIFFERENT task is different work, not a rework.
    assert kinds[13] == "rk#1"
    # Untasked rows group by step alone.
    assert kinds[14] == "rk#1"
    assert kinds[17] == "rk#2"
    assert kinds[15] == "rk#1"


def test_card_totals_per_step_and_holder(monkeypatch, tmp_path, capsys):
    rc, _ = _run(["--card", "1765"], monkeypatch, tmp_path,
                 obligations=_reworked_card_rows())
    assert rc == 0
    out = capsys.readouterr().out
    # build: 5 rows, closed held 2h+1h+1h+1h = 5h over 4 measured,
    # 1 unmeasured (close with no take).
    assert "  build: 5 row(s), closed 5, held 5h00m over 4 measured, 1 unmeasured" in out
    # validate: 2 open rows; open-held 13h over 1, open-age 18h over 2.
    assert "  validate: 2 row(s), closed 0, held 0s over 0 measured" in out
    assert "open-held* 13h00m over 1" in out
    assert "open-age* 18h00m over 2" in out
    # alice holds 11, 12, 14, 16, 17, 18: 6 rows.
    assert "  alice: 6 row(s)" in out
    # commander-held is its own section, holder == 'commander'.
    assert "commander-held (holder == 'commander'):" in out
    assert "  commander: 1 row(s), closed 1, held 1h30m over 1 measured" in out


def test_missing_timestamps_render_dash_without_crash(monkeypatch, tmp_path,
                                                      capsys):
    rc, _ = _run(["--card", "1765"], monkeypatch, tmp_path,
                 obligations=_reworked_card_rows())
    assert rc == 0
    out = capsys.readouterr().out
    line16 = next(line for line in out.splitlines()
                  if line.startswith("  #16 "))
    assert "taken=-" in line16 and "held=-" in line16
    line18 = next(line for line in out.splitlines()
                  if line.startswith("  #18 "))
    assert "created=-" in line18 and "held=1h" in line18
    line17 = next(line for line in out.splitlines()
                  if line.startswith("  #17 "))
    assert "outcome=-" in line17


def test_card_json_parses_with_rework_and_durations(monkeypatch, tmp_path,
                                                    capsys):
    rc, _ = _run(["--card", "1765", "--json"], monkeypatch, tmp_path,
                 obligations=_reworked_card_rows())
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["card_id"] == 1765
    by_id = {r["id"]: r for r in parsed["rows"]}
    assert [r["id"] for r in parsed["rows"]] == [11, 12, 13, 15, 16, 14, 17, 18]
    assert by_id[11]["rework"] == 1 and by_id[12]["rework"] == 2
    assert by_id[13]["rework"] == 1
    assert by_id[11]["held_s"] == 7200.0
    assert by_id[16]["held_s"] is None
    assert by_id[14]["held_open_s"] == 13 * 3600.0
    assert parsed["totals"]["commander"]["rows"] == 1


def test_no_such_card_is_a_clean_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")

    def fake_get(url, token, *, timeout=10.0):
        if "/board/obligations" in url:
            return 200, {"obligations": [], "as_of": AS_OF}
        return 404, {"detail": "no such card"}

    monkeypatch.setattr(usage_cmd, "_http_get_json", fake_get)
    rc = usage_cmd.run_usage(["timeline", "--card", "999999", "--as",
                              "meta-muse", "--gateway", "http://gw:8788"])
    assert rc == 1
    assert "no such card #999999" in capsys.readouterr().err


def test_gateway_failure_is_nonzero_without_leak(monkeypatch, tmp_path,
                                                 capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    monkeypatch.setattr(usage_cmd, "_http_get_json",
                        lambda url, token, *, timeout=10.0:
                        (0, {"detail": "connection refused"}))
    rc = usage_cmd.run_usage(["timeline", "--card", "1765", "--as",
                              "meta-muse", "--gateway", "http://gw:8788"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "gateway unreachable" in err
    assert "shared-auth" not in err


# ── mode B: peer day ────────────────────────────────────────────────────

def test_peer_day_boundary_at_utc_midnight(monkeypatch, tmp_path, capsys):
    rc, _ = _run(["--peer", "friendly-coder", "--day", "2026-11-27"],
                 monkeypatch, tmp_path, obligations=_peer_day_rows())
    assert rc == 0
    out = capsys.readouterr().out
    assert "peer friendly-coder on 2026-11-27 (UTC): 6 row(s)" in out
    assert "  #21 " not in out  # taken 23:59:59Z the day before
    assert "  #27 " not in out  # never taken nor closed
    for rid in (22, 23, 24, 25, 26, 28):
        assert f"  #{rid} " in out


def test_peer_roles_and_single_line_for_both(monkeypatch, tmp_path, capsys):
    rc, _ = _run(["--peer", "friendly-coder", "--day", "2026-11-27"],
                 monkeypatch, tmp_path, obligations=_peer_day_rows())
    assert rc == 0
    out = capsys.readouterr().out
    assert "  #22 card=1765 took " in out
    assert "  #23 card=1765 closed " in out
    assert "  #24 card=1765 took+closed " in out
    assert out.count("  #24 card=") == 1
    # Closed exactly at next midnight belongs to the NEXT day, not this one.
    assert "  #25 card=1765 took " in out
    # A row on another card still lists (mode B spans cards).
    assert "  #28 card=1770 took " in out


def test_peer_sort_is_event_then_id_and_durations(monkeypatch, tmp_path,
                                                  capsys):
    rc, _ = _run(["--peer", "friendly-coder", "--day", "2026-11-27"],
                 monkeypatch, tmp_path, obligations=_peer_day_rows())
    assert rc == 0
    out = capsys.readouterr().out
    ids = [int(line.split()[0][1:])
           for line in out.splitlines() if line.startswith("  #")]
    assert ids == [22, 24, 25, 28, 23, 26]
    line24 = next(line for line in out.splitlines()
                  if line.startswith("  #24 "))
    assert "held=1h30m" in line24
    line26 = next(line for line in out.splitlines()
                  if line.startswith("  #26 "))
    assert "taken=-" in line26 and "held=-" in line26
    line22 = next(line for line in out.splitlines()
                  if line.startswith("  #22 "))
    assert "held=1d00h*" in line22
    # Row 25 closed the next day but taken->closed is fully determined, so its
    # 14h joins the measured held total; rows 22+28 contribute open-held.
    assert "total held 7d17h30m over 3 measured" in out
    assert "open-held* 1d13h over 2 (1 unmeasured)" in out
    assert "(1 unmeasured)" in out


def test_peer_json_parses(monkeypatch, tmp_path, capsys):
    rc, _ = _run(["--peer", "friendly-coder", "--day", "2026-11-27",
                  "--json"], monkeypatch, tmp_path,
                 obligations=_peer_day_rows())
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["peer"] == "friendly-coder" and parsed["day"] == "2026-11-27"
    by_id = {r["id"]: r for r in parsed["rows"]}
    assert by_id[24]["role"] == "took+closed"
    assert by_id[24]["held_s"] == 5400.0
    assert by_id[26]["held_s"] is None


# ── argument handling ───────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["--peer", "p"],                       # peer without day
    ["--day", "2026-11-27"],               # day without peer
    ["--card", "1765", "--peer", "p", "--day", "2026-11-27"],
    ["--card", "1765", "--day", "2026-11-27"],
    ["--card", "1765", "--peer", "p"],
    ["--card", "1765", "--day", "not-a-day"],
    ["--peer", "p", "--day", "2026-13-40"],
    ["--card", "1765", "--as-of", "yesterday"],
    [],                                    # neither mode
])
def test_invalid_modes_exit_2(argv, monkeypatch, tmp_path, capsys):
    rc, _ = _run(argv, monkeypatch, tmp_path,
                 obligations=_reworked_card_rows())
    assert rc == 2


def test_non_integer_card_exits_2(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        usage_cmd.run_usage(["timeline", "--card", "abc"])
    assert exc.value.code == 2


def test_timeline_asks_for_gateway_url_shape(monkeypatch, tmp_path):
    """The timeline path reuses the board URL builders exactly."""
    rc, calls = _run(["--card", "1765"], monkeypatch, tmp_path,
                     obligations=_reworked_card_rows())
    assert rc == 0
    assert any("/board/obligations?card_id=1765" in url for url in calls)
    assert any(url.endswith("/board/cards/1765") for url in calls)
    rc, calls = _run(["--peer", "friendly-coder", "--day", "2026-11-27"],
                     monkeypatch, tmp_path, obligations=_peer_day_rows())
    assert rc == 0
    assert any("/board/obligations?holder=friendly-coder" in url
               for url in calls)


# ── no-write regression guard ───────────────────────────────────────────

def test_timeline_path_has_no_write_verbs():
    """Static: the timeline modules name no write transport. Behavioral
    coverage is every test above — the fake transport only answers GETs."""
    import re
    from pathlib import Path
    text = Path(tl.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"_post_json|_patch_json|urlopen\s*\(|requests\.|"
        r"method\s*=\s*[\"'](?:POST|PATCH|PUT|DELETE)", text)
    timeline_src = inspect.getsource(usage_cmd._run_timeline)
    assert "_post_json" not in timeline_src
    assert "_patch_json" not in timeline_src
    assert "POST /board" not in timeline_src
