"""#307 Task 4 — the CLI surface for minting an obligation and closing it.

Tasks 1/2/5 built the row, the close, and the surface INSIDE the gateway. None of
them is reachable by a person. This is the half an operator actually types, and the
card's failure mode was never a missing table — lab said "#91 is waiting on a seat"
three times over four hours because ASKING was not an act that left a record.

TWO PROPERTIES THIS FILE EXISTS TO PIN, both of which a happy-path test would miss:

1. >>> A THREADLESS REPLY AND A THREADED ONE MUST NOT PRINT THE SAME LINE. <<< Most
   DMs carry no thread. Refusing them would make `reply` useless for the common case;
   sending them silently would let an operator believe they had closed an obligation
   when nothing could have. Both are sent, and the two outcomes are DISTINGUISHABLE
   in the output. One message for two causes hides which one happened.

2. >>> AN OBLIGATION WITH NO TIMEOUT MUST SAY SO AT CREATION. <<< #145's lesson one
   layer up: "0 overdue" must never be able to mean "nobody set dates". After the
   fact, an obligation that can never go red looks exactly like one that is not late
   yet — so the only honest moment to say it is when the operator mints it.

`reply` is NOT gated by kind, per the spec's standing constraint, and that has its
own test: a gate would let a peer owe a delivery on a DM whose kind nobody thought
to allow, leaving the obligation unclosable through the product — the precise
"waiting on a seat" shape the card exists to kill.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import board, mesh


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setattr(mesh, "_resolve_self_name", lambda a: a or "lab-ovh")
    monkeypatch.setattr(mesh, "_resolve_token", lambda n, f: "tok")
    monkeypatch.setattr(board, "_resolve_self_name", lambda a: a or "lab-ovh",
                        raising=False)


def _posts(monkeypatch, mod, status=200, payload=None):
    """Capture (url, body) and return a canned gateway response."""
    seen = []

    def fake(url, body, token):
        seen.append((url, body))
        return status, (payload if payload is not None else {"id": 7})

    monkeypatch.setattr(mod, "_post_json", fake)
    return seen


# ── ask ─────────────────────────────────────────────────────────────────────

def test_ask_posts_to_the_ask_endpoint(monkeypatch, capsys):
    seen = _posts(monkeypatch, board, payload={
        "id": 1, "card_id": 42, "holder": "droplet", "status": "open",
        "timeout_at": None, "thread_uuid": "t1"})

    rc = board.run_board(["cards", "ask", "42", "droplet", "please review",
                          "--gateway", "http://gw", "--as", "lab-ovh"])

    assert rc == 0
    url, body = seen[0]
    assert url == "http://gw/board/cards/42/ask"
    assert body["holder"] == "droplet"
    assert body["what"] == "please review"
    assert body["created_by"] == "lab-ovh"


def test_ask_SAYS_when_the_obligation_can_never_go_red(monkeypatch, capsys):
    """>>> #145's LESSON, ONE LAYER UP. <<< An obligation with no timeout never goes
    overdue on its own. Afterwards it is indistinguishable from one that simply is
    not late yet, so creation is the only honest moment to say it."""
    _posts(monkeypatch, board, payload={
        "id": 1, "card_id": 42, "holder": "droplet", "status": "open",
        "timeout_at": None, "thread_uuid": "t1"})

    board.run_board(["cards", "ask", "42", "droplet", "review",
                     "--gateway", "http://gw", "--as", "lab-ovh"])

    out = capsys.readouterr().out
    assert "NO TIMEOUT" in out and "--timeout-hours" in out


def test_ask_with_a_timeout_names_the_deadline_instead(monkeypatch, capsys):
    seen = _posts(monkeypatch, board, payload={
        "id": 1, "card_id": 42, "holder": "droplet", "status": "open",
        "timeout_at": "2026-08-19T00:00:00Z", "thread_uuid": "t1"})

    board.run_board(["cards", "ask", "42", "droplet", "review", "--timeout-hours", "24",
                     "--gateway", "http://gw", "--as", "lab-ovh"])

    assert seen[0][1]["timeout_hours"] == 24
    out = capsys.readouterr().out
    assert "2026-08-19T00:00:00Z" in out and "NO TIMEOUT" not in out


# ── reply ───────────────────────────────────────────────────────────────────

def _inbox(monkeypatch, messages):
    monkeypatch.setattr(mesh, "_http_get_json",
                        lambda url, token: (200, {"messages": messages}))


def test_reply_posts_IN_THE_ORIGINAL_THREAD(monkeypatch, capsys):
    """The whole mechanism: Task 2 closes on a reply carrying the obligation's
    thread_id. Drop the thread_id and the obligation never closes."""
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": "t-abc"}])
    seen = _posts(monkeypatch, mesh, payload={"id": 100})

    rc = mesh.run_mesh(["reply", "99", "--content", "done", "--gateway", "http://gw"])

    assert rc == 0
    url, body = seen[0]
    assert url == "http://gw/messages"
    assert body["thread_id"] == "t-abc"
    assert body["to_node"] == "droplet", "a reply goes to the ORIGINAL SENDER"
    assert body["kind"] == "answer"


def test_a_THREADLESS_reply_is_SENT_but_says_it_closes_nothing(monkeypatch, capsys):
    """>>> THE PROPERTY THAT MATTERS MOST HERE. <<< Sending silently would let an
    operator believe an obligation closed when nothing could have. Refusing would
    make the verb useless for the common case. Send, and SAY WHICH."""
    _inbox(monkeypatch, [{"id": 99, "from_node": "droplet", "thread_id": None}])
    seen = _posts(monkeypatch, mesh, payload={"id": 100})

    rc = mesh.run_mesh(["reply", "99", "--content", "ok", "--gateway", "http://gw"])

    assert rc == 0, "a threadless reply must still be SENT"
    assert "thread_id" not in seen[0][1]
    out = capsys.readouterr().out
    assert "NOT IN A THREAD" in out and "closes no obligation" in out


def test_the_two_reply_outcomes_do_not_print_the_same_line(monkeypatch, capsys):
    """Stated as its own test because it is the actual invariant. The two branches
    above could each pass while emitting identical text if someone 'simplified' the
    message later — one message for two causes hides which."""
    _inbox(monkeypatch, [{"id": 1, "from_node": "d", "thread_id": "t"}])
    _posts(monkeypatch, mesh, payload={"id": 10})
    mesh.run_mesh(["reply", "1", "--content", "x", "--gateway", "http://gw"])
    threaded = capsys.readouterr().out

    _inbox(monkeypatch, [{"id": 2, "from_node": "d", "thread_id": None}])
    _posts(monkeypatch, mesh, payload={"id": 11})
    mesh.run_mesh(["reply", "2", "--content", "x", "--gateway", "http://gw"])
    threadless = capsys.readouterr().out

    assert threaded != threadless


@pytest.mark.parametrize("kind", ["answer", "fyi", "status", "question", "unblock"])
def test_reply_is_NOT_gated_by_kind(monkeypatch, kind):
    """Spec constraint: universal across every DM kind. A gate would let a peer owe a
    delivery on a kind nobody thought to allow, leaving the obligation unclosable
    through the product — the 'waiting on a seat' shape, rebuilt."""
    _inbox(monkeypatch, [{"id": 5, "from_node": "d", "thread_id": "t", "kind": kind}])
    seen = _posts(monkeypatch, mesh, payload={"id": 6})

    rc = mesh.run_mesh(["reply", "5", "--content", "x", "--kind", kind,
                        "--gateway", "http://gw"])

    assert rc == 0 and seen[0][1]["kind"] == kind


def test_a_MISSING_message_refusal_names_the_SEARCH_BOUND(monkeypatch, capsys):
    """>>> THERE IS NO GET /messages/{id}, SO THE SEARCH IS BOUNDED. <<< A message
    older than the window is indistinguishable from one that never existed unless the
    refusal says what was searched — otherwise the operator hunts a delivery bug that
    is really a paging window."""
    _inbox(monkeypatch, [{"id": 1, "from_node": "d", "thread_id": "t"}])
    _posts(monkeypatch, mesh)

    rc = mesh.run_mesh(["reply", "404", "--content", "x", "--gateway", "http://gw"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "search-limit" in err and "older than that window" in err


def test_a_reply_is_NOT_sent_when_the_original_cannot_be_found(monkeypatch):
    """The refusal must be a real abort. A warning followed by a send would deliver a
    DM to nobody in particular, or worse, to a guessed recipient."""
    _inbox(monkeypatch, [])
    seen = _posts(monkeypatch, mesh)

    mesh.run_mesh(["reply", "404", "--content", "x", "--gateway", "http://gw"])

    assert seen == [], "nothing may be posted when the target is unknown"
