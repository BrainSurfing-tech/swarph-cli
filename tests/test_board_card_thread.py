"""#181 — the card-thread CLI verbs (`board cards thread` / `board cards say`).

The gateway carried GET /board/cards/{id}/thread and the card-gated attach path
from 2026-08-05 and NOTHING COULD REACH THEM: no CLI verb existed, so the card↔DM
fusion lived in the database and the OpenAPI schema and nowhere a person could
touch. These tests pin the CLI half.

Pure helpers are tested directly; HTTP is the seam (monkeypatched), matching
tests/test_board.py. The live round-trip (post → read back) was verified by
execution against the running gateway on 2026-08-05 — id=16229 onto card #314 —
which is the check no mock can make.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import board


# ── _thread_url ───────────────────────────────────────────────────────────────

def test_thread_url_without_limit():
    assert board._thread_url("http://gw:8788", 12) == "http://gw:8788/board/cards/12/thread"


def test_thread_url_with_limit_and_trailing_slash():
    assert board._thread_url("http://gw:8788/", 12, limit=5) == \
        "http://gw:8788/board/cards/12/thread?limit=5"


# ── _thread_recipient — the refusal is the point ──────────────────────────────

def test_recipient_prefers_explicit_to_over_assignee():
    assert board._thread_recipient({"id": 1, "assignee": "droplet"}, "gpt-ops") == "gpt-ops"


def test_recipient_defaults_to_assignee():
    assert board._thread_recipient({"id": 1, "assignee": "droplet"}, None) == "droplet"


def test_recipient_RAISES_rather_than_inventing_a_sentinel():
    """THE LOAD-BEARING TEST. POST /messages needs exactly one of {to_node,
    channel}, so the tempting shortcut is a placeholder like "board" or
    "__card__". Board card #259 measured what the gateway does with an
    unregistered to_node: 200, addressed to nobody, byte-identical to delivery.
    A placeholder here would manufacture that defect once per card post.
    """
    with pytest.raises(RuntimeError) as exc:
        board._thread_recipient({"id": 42, "assignee": None}, None)
    msg = str(exc.value)
    assert "--to" in msg          # tells the caller how to proceed
    assert "42" in msg            # names the card rather than failing abstractly


@pytest.mark.parametrize("assignee", ["", None])
def test_recipient_treats_blank_assignee_as_absent(assignee):
    # "" is falsy in Python but a VALID to_node as far as the wire is concerned —
    # min_length=1 would 422 server-side, which is a worse error than ours.
    with pytest.raises(RuntimeError):
        board._thread_recipient({"id": 7, "assignee": assignee}, None)


# ── _card_say_payload ─────────────────────────────────────────────────────────

def test_say_payload_carries_the_thread_binding():
    p = board._card_say_payload("lab-ovh", "droplet", "answer", "hi", "uuid-1")
    assert p == {"from_node": "lab-ovh", "to_node": "droplet", "kind": "answer",
                 "content": "hi", "thread_id": "uuid-1"}
    # thread_id is what makes this a CARD post rather than an ad-hoc DM. Without
    # it the message is delivered and simply never appears on the card — the
    # silent-success shape this whole card family exists to remove.
    assert p["thread_id"] == "uuid-1"


# ── _format_thread — empty must not look like unreadable ──────────────────────

def test_format_thread_empty_says_empty_explicitly():
    out = board._format_thread({"card_id": 9, "messages": []})
    assert "9" in out and "empty" in out.lower()


def test_format_thread_renders_sender_recipient_and_body():
    out = board._format_thread({"card_id": 9, "messages": [
        {"id": 5, "created_at": "2026-08-05T12:28:00Z", "from_node": "lab-ovh",
         "to_node": "droplet", "kind": "answer", "content": "line one\nline two"},
    ]})
    assert "card #9" in out and "1 message" in out
    assert "lab-ovh -> droplet" in out and "(answer)" in out
    assert "line one" in out and "line two" in out    # multi-line body survives


# ── dispatch: a refusal must reach the operator, not be flattened ─────────────

def _run(monkeypatch, *, get=None, post=None, argv):
    monkeypatch.setattr(board, "_resolve_self_name", lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board, "_resolve_token", lambda *_a, **_k: "tok")
    if get is not None:
        monkeypatch.setattr(board, "_http_get_json", get)
    if post is not None:
        monkeypatch.setattr(board, "_post_json", post)
    return board.run_board(argv)


def test_thread_409_is_surfaced_by_name_not_rendered_as_empty(monkeypatch, capsys):
    """An unmigrated card 409s. If the CLI rendered that as "(no messages)" it
    would assert "nobody has discussed this card" — a DIFFERENT and false claim
    the operator cannot distinguish from the truth.

    NOTE ON COVERAGE: this branch is unit-tested only. The 2026-08-05 migration
    bound a thread to all ~300 live cards, so the 409 condition can no longer be
    produced against the real gateway without writing to the DB directly — which
    is board #313's defect, and demonstrating it to test a message is not a trade
    worth making. Unit-tested, not live-tested, stated rather than implied.
    """
    rc = _run(monkeypatch,
              get=lambda url, tok, **k: (409, {"detail": "card 7 predates #181a"}),
              argv=["cards", "thread", "7"])
    assert rc == 1
    assert "predates #181a" in capsys.readouterr().err


def test_thread_403_is_surfaced_not_flattened(monkeypatch, capsys):
    rc = _run(monkeypatch,
              get=lambda url, tok, **k: (403, {"detail": "card 7 is not readable by you"}),
              argv=["cards", "thread", "7"])
    assert rc == 1
    assert "not readable" in capsys.readouterr().err


def test_say_passes_the_gateways_403_reason_through_whole(monkeypatch, capsys):
    """The gateway's 403 explains that attaching PUBLISHES to everyone who can
    read the card, now and in future, so it needs an explicit `propose` grant.
    A shortened "forbidden" would drop exactly the part that tells the operator
    what to ask for."""
    detail = ("thread X is bound to board card 7; attaching a message to a card "
              "thread publishes it to everyone who can read that card — now and "
              "in future — so it requires an explicit `propose` grant")
    rc = _run(monkeypatch,
              get=lambda url, tok, **k: (200, {"id": 7, "assignee": "droplet",
                                               "thread_uuid": "X"}),
              post=lambda url, body, tok, **k: (403, {"detail": detail}),
              argv=["cards", "say", "7", "--content", "hi"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "propose" in err and "publishes it to everyone" in err


def test_say_refuses_a_card_with_no_bound_thread(monkeypatch, capsys):
    rc = _run(monkeypatch,
              get=lambda url, tok, **k: (200, {"id": 7, "assignee": "droplet",
                                               "thread_uuid": None}),
              argv=["cards", "say", "7", "--content", "hi"])
    assert rc == 1
    assert "no bound thread" in capsys.readouterr().err


def test_say_posts_with_thread_id_and_defaults_to_assignee(monkeypatch, capsys):
    seen = {}
    def _post(url, body, tok, **k):
        seen["url"], seen["body"] = url, body
        return 200, {"id": 999}
    rc = _run(monkeypatch,
              get=lambda url, tok, **k: (200, {"id": 7, "assignee": "droplet",
                                               "thread_uuid": "uuid-7"}),
              post=_post,
              argv=["cards", "say", "7", "--content", "hi", "--kind", "answer"])
    assert rc == 0
    assert seen["url"].endswith("/messages")
    assert seen["body"]["thread_id"] == "uuid-7"     # the card binding
    assert seen["body"]["to_node"] == "droplet"      # defaulted, not invented
    assert "999" in capsys.readouterr().out
