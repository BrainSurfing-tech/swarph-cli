"""Card #1795 item 3 — render `already open: #{n}` when ask dedups onto an
existing open obligation row (`already_open: true`).
"""
from __future__ import annotations

from swarph_cli.commands import board


def _resp(**kw):
    base = {
        "id": 91,
        "card_id": 20,
        "holder": "cursor-win",
        "status": "open",
        "state": "offered",
        "taken_at": None,
        "timeout_at": None,
        "thread_uuid": "thread-91",
        "accept": "PASS = x | FAIL = y",
        "step": "validate",
        "needs": ["build"],
        "menu_version": 3,
        "eligible": None,
        "take_with": "swarph board obligations take 91",
    }
    base.update(kw)
    return base


# ── _ask_line with already_open=True ─────────────────────────────────────────

def test_ask_line_already_open_offered_unstepped_preserves_details():
    warn = "WARN: no --step — row #91 is `unstepped`: it buys no gate credit"
    out = board._ask_line(_resp(
        state="offered",
        holder="alice",
        step=None,
        needs=None,
        menu_version=None,
        accept=None,
        timeout_at=None,
        warn=warn,
        already_open=True,
    ))
    lines = out.splitlines()
    assert lines[0] == (
        "already open: #91 offered to alice (unstepped on #20) — "
        "alice takes with: swarph board obligations take 91"
    )
    assert "minted #91" not in out
    assert "NO ACCEPT CHECK" in lines[1]
    assert lines[2].strip() == board._NO_TIMEOUT
    assert "thread thread-91" in lines[3]
    assert "swarph board obligations close 91" in lines[3]
    assert lines[-1] == warn


def test_ask_line_already_open_requested_and_other_states():
    req = board._ask_line(_resp(
        state="requested",
        holder=None,
        eligible=["cursor-win", "drop"],
        already_open=True,
    ))
    req_lines = req.splitlines()
    assert req_lines[0].startswith("already open: #91 requested (validate on #20)")
    assert "minted #91" not in req
    assert "accept: PASS = x | FAIL = y" in req_lines[1]

    other = board._ask_line(_resp(
        state="blocked",
        holder="cursor-win",
        already_open=True,
    ))
    assert other.splitlines()[0] == "already open: #91 validate on #20 · blocked"
    assert "minted #91" not in other


def test_ask_line_already_open_state_open_replaces_open_prefix_and_keeps_details():
    out = board._ask_line(_resp(
        state="open",
        holder="cursor-win",
        step=None,
        due="+24h after build closes",
        accept="verify it works",
        timeout_at="2026-09-04T00:00:00Z",
        warn="WARN: unstepped",
        already_open=True,
    ))
    lines = out.splitlines()
    assert lines[0] == (
        "already open: #91 (cursor-win, unstepped on #20, due +24h after build closes) "
        "— close with: swarph board obligations close 91 --outcome pass --evidence \"<container>\""
    )
    assert "#91 open" not in lines[0]
    assert "minted #91" not in out
    assert "NO FAIL BRANCH" in lines[1]
    assert "thread thread-91" in lines[2]
    assert lines[-1] == "WARN: unstepped"


# ── _ask_line with already_open=False or omitted (byte-identical) ────────────

def test_ask_line_already_open_false_or_omitted_matches_exact_existing_stepped():
    for state_kwargs in (
        {"state": "requested", "holder": None, "eligible": ["cursor-win", "drop"]},
        {"state": "offered", "holder": "cursor-win"},
        {"state": "open", "holder": "cursor-win", "due": "2026-09-03T10:00:00Z"},
        {"state": "closed:pass", "holder": "cursor-lin", "status": "closed"},
    ):
        omitted = board._ask_line(_resp(**state_kwargs))
        explicit_false = board._ask_line(_resp(already_open=False, **state_kwargs))
        assert explicit_false == omitted
        assert "already open" not in omitted


def test_ask_line_already_open_false_or_omitted_matches_exact_stepless():
    warn = "WARN: no --step — row #91 is `unstepped`: it buys no gate credit"
    for state_kwargs in (
        {"state": "offered", "holder": "alice", "step": None, "accept": None, "warn": warn},
        {"state": "open", "holder": "alice", "step": None, "accept": None, "warn": warn},
        {"state": "requested", "holder": None, "eligible": ["alice"], "step": None, "accept": None},
    ):
        omitted = board._ask_line(_resp(**state_kwargs))
        explicit_false = board._ask_line(_resp(already_open=False, **state_kwargs))
        assert explicit_false == omitted
        assert "already open" not in omitted


# ── _format_ask (responses without `state`) ──────────────────────────────────

def test_format_ask_already_open_true_and_false():
    base = {
        "id": 7,
        "card_id": 42,
        "holder": "droplet",
        "status": "open",
        "timeout_at": None,
        "thread_uuid": "t-7",
        "accept": "PASS = a | FAIL = b",
    }
    out_true = board._format_ask({**base, "already_open": True})
    lines_true = out_true.splitlines()
    assert lines_true[0] == (
        "already open: #7 on card #42: droplet owes it, status=open, " + board._NO_TIMEOUT
    )
    assert "  accept: PASS = a | FAIL = b" == lines_true[1]
    assert "thread t-7" in lines_true[2]
    assert board._ask_line({**base, "already_open": True}) == out_true

    out_omitted = board._format_ask(base)
    out_false = board._format_ask({**base, "already_open": False})
    assert out_false == out_omitted
    assert out_omitted.splitlines()[0].startswith("obligation #7 on card #42:")


# ── End-to-end CLI dispatch ──────────────────────────────────────────────────

def test_cli_cards_ask_already_open_prints_prefix_and_exits_0(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(board, "_resolve_self_name", lambda s: s or "lab-ovh")
    monkeypatch.setattr(board, "_resolve_token", lambda n, f: "tok")

    def fake_post(url, body, token):
        seen.append((url, body, token))
        return 200, {
            "id": 404,
            "card_id": 1795,
            "holder": "droplet",
            "status": "open",
            "state": "offered",
            "timeout_at": None,
            "thread_uuid": "t-404",
            "accept": None,
            "step": None,
            "take_with": "swarph board obligations take 404",
            "already_open": True,
            "warn": "WARN: deduped onto existing open row #404",
        }

    monkeypatch.setattr(board, "_post_json", fake_post)

    rc = board.run_board([
        "cards", "ask", "1795", "droplet", "please check",
        "--gateway", "http://gw", "--as", "lab-ovh",
    ])
    assert rc == 0
    assert len(seen) == 1
    assert seen[0][0] == "http://gw/board/cards/1795/ask"
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("already open: #404 offered to droplet (unstepped on #1795)")
    assert "minted #404" not in out
    assert "NO ACCEPT CHECK" in out
    assert board._NO_TIMEOUT in out
    assert "thread t-404" in out
    assert out.splitlines()[-1] == "WARN: deduped onto existing open row #404"
