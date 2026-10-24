"""Card #931. `swarph board cards list --search TEXT` asks the gateway (q=), and says
how many cards were searched.

Two traps this pins:
  - the gateway does the matching. The CLI must send `q` and must not pull every
    card and filter here;
  - a gateway older than #931 silently IGNORES an unknown `q` and returns every
    card, so the list looks filtered and is not. No `searched` in the reply means
    the search did not happen: refuse, do not render it.
"""
import json

import pytest

from swarph_cli.commands import board


def test_cards_list_url_carries_the_search_text_as_q():
    url = board._cards_list_url("http://gw:8788", search="loose ferrule & 100%")
    assert url == "http://gw:8788/board/cards?q=loose+ferrule+%26+100%25"
    assert "q=" not in board._cards_list_url("http://gw:8788", stage="idea")


def _run(monkeypatch, argv, reply):
    sent = {}
    monkeypatch.setattr(board, "_resolve_self_name", lambda *_a, **_k: "cursor-lin")
    monkeypatch.setattr(board, "_resolve_token", lambda *_a, **_k: "tok")

    def fake_get(url, tok, **k):
        sent["url"] = url
        return reply
    monkeypatch.setattr(board, "_http_get_json", fake_get)
    return board.run_board(argv), sent


_CARD = {"id": 7, "stage": "build", "project_id": 1, "ai2": False,
         "title": "wire the tunnel"}


def test_search_sends_q_and_prints_the_searched_count(monkeypatch, capsys):
    rc, sent = _run(monkeypatch, ["cards", "list", "--search", "ferrule"],
                    (200, {"cards": [_CARD], "searched": 412}))
    out = capsys.readouterr().out
    assert rc == 0
    assert "q=ferrule" in sent["url"]
    assert "searched 412 cards: 1 match" in out
    assert "wire the tunnel" in out


def test_a_zero_says_how_many_cards_it_looked_at(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, ["cards", "list", "--search", "zzz"],
                 (200, {"cards": [], "searched": 412}))
    out = capsys.readouterr().out
    assert rc == 0
    assert "(no cards)" in out and "searched 412 cards: 0 match" in out
    # a partial cover must not read like the same zero
    rc, _ = _run(monkeypatch, ["cards", "list", "--search", "zzz", "--stage", "idea"],
                 (200, {"cards": [], "searched": 3}))
    assert "searched 3 cards: 0 match" in capsys.readouterr().out


def test_json_output_carries_searched(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, ["cards", "list", "--search", "ferrule", "--json"],
                 (200, {"cards": [_CARD], "searched": 412}))
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["searched"] == 412


def test_a_gateway_that_ignored_q_is_refused_not_rendered(monkeypatch, capsys):
    rc, _ = _run(monkeypatch, ["cards", "list", "--search", "ferrule"],
                 (200, {"cards": [_CARD, dict(_CARD, id=8)]}))
    cap = capsys.readouterr()
    assert rc == 1
    assert "wire the tunnel" not in cap.out, "an unfiltered list was rendered as a result"
    assert "searched" in cap.err and "ignored" in cap.err


def test_no_search_leaves_the_list_alone(monkeypatch, capsys):
    rc, sent = _run(monkeypatch, ["cards", "list"], (200, {"cards": [_CARD]}))
    out = capsys.readouterr().out
    assert rc == 0 and "q=" not in sent["url"] and "searched" not in out


def test_a_blank_search_still_reaches_the_gateway_to_be_refused(monkeypatch, capsys):
    rc, sent = _run(monkeypatch, ["cards", "list", "--search", " "],
                    (400, {"detail": "q is blank"}))
    assert rc == 1 and "q=+" in sent["url"]
    assert "q is blank" in capsys.readouterr().err
