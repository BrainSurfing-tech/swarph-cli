"""Tests for the ``swarph committee`` verb (card #1323).

The module binds the gateway-client helpers by name, so the fakes are patched
as attributes of the ``committee`` module. The motion id is always discovered
through the GET list route, never scanned.
"""

from __future__ import annotations

import json

import pytest

from swarph_cli.commands import committee as cm


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "c1")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")


def _spy(monkeypatch, name, status=200, payload=None):
    seen: dict = {}

    def fake(url, *args, **kwargs):
        seen.setdefault("calls", []).append(url)
        seen["url"] = url
        if args and isinstance(args[0], dict):
            seen["body"] = args[0]
        return status, (payload if payload is not None else {})

    monkeypatch.setattr(cm, name, fake)
    return seen


def _open_motion_payload(motion_id=7):
    return {"motions": [{
        "id": motion_id, "channel": "fusion-aid", "phase": "positions",
        "positions_deadline": "2026-10-28T12:00:00+00:00",
        "rebuttal_deadline": "2026-10-29T12:00:00+00:00",
        "vote_deadline": "2026-10-30T12:00:00+00:00",
    }]}


def test_committee_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["committee"] == "swarph_cli.commands.committee.run_committee"


def test_no_subcommand_prints_help():
    assert cm.run_committee([]) == 0


def test_motion_lists_the_open_motion_without_scanning(monkeypatch, capsys):
    seen = _spy(monkeypatch, "get_json", payload=_open_motion_payload())
    rc = cm.run_committee(["motion", "fusion-aid"])
    assert rc == 0
    assert seen["url"].endswith("/committee/fusion-aid/motions")
    out = capsys.readouterr().out
    assert "motion 7 phase=positions" in out
    assert "positions_deadline=2026-10-28" in out


def test_motion_open_posts_the_motion(monkeypatch):
    seen = _spy(monkeypatch, "post_json", payload={"id": 7, "phase": "positions"})
    rc = cm.run_committee([
        "motion", "fusion-aid", "--open",
        "--text", "Adopt?", "--options", "Approve,Amend,Reject",
        "--positions-deadline", "2026-10-28T12:00:00+00:00",
        "--rebuttal-deadline", "2026-10-29T12:00:00+00:00",
        "--vote-deadline", "2026-10-30T12:00:00+00:00",
    ])
    assert rc == 0
    assert seen["url"].endswith("/committee/fusion-aid/motions")
    assert seen["body"]["options"] == ["Approve", "Amend", "Reject"]
    assert seen["body"]["text"] == "Adopt?"


def test_motion_open_without_flags_is_a_usage_error(monkeypatch, capsys):
    rc = cm.run_committee(["motion", "fusion-aid", "--open"])
    assert rc == 2
    assert "--text" in capsys.readouterr().err


def test_post_discovers_the_motion_and_posts_a_sealed_position(monkeypatch, tmp_path):
    body = tmp_path / "pos.txt"
    body.write_text("I hold the motion.", encoding="utf-8")
    _spy(monkeypatch, "get_json", payload=_open_motion_payload())
    seen = _spy(monkeypatch, "post_json", payload={
        "motion_id": 7, "phase": "positions", "message_id": 99})
    rc = cm.run_committee(["post", "fusion-aid", "--round", "positions",
                           "--body-file", str(body)])
    assert rc == 0
    assert seen["url"].endswith("/committee/fusion-aid/motions/7/posts")
    assert seen["body"] == {"round": "positions", "body": "I hold the motion.",
                            "outcome": "pass"}


def test_post_rebuttal_carries_no_outcome(monkeypatch, tmp_path):
    body = tmp_path / "reb.txt"
    body.write_text("Objection.", encoding="utf-8")
    _spy(monkeypatch, "get_json", payload=_open_motion_payload())
    seen = _spy(monkeypatch, "post_json", payload={"motion_id": 7, "phase": "rebuttal"})
    rc = cm.run_committee(["post", "fusion-aid", "--round", "rebuttal",
                           "--body-file", str(body)])
    assert rc == 0
    assert "outcome" not in seen["body"]
    assert seen["body"]["round"] == "rebuttal"


def test_post_non_member_surfaces_the_gateway_refusal(monkeypatch, tmp_path, capsys):
    body = tmp_path / "pos.txt"
    body.write_text("x", encoding="utf-8")
    _spy(monkeypatch, "get_json", payload=_open_motion_payload())
    _spy(monkeypatch, "post_json", status=403,
         payload={"detail": "c1 is not a member of channel fusion-aid"})
    rc = cm.run_committee(["post", "fusion-aid", "--round", "positions",
                           "--body-file", str(body)])
    assert rc == 1
    assert "not a member of channel fusion-aid" in capsys.readouterr().err


def test_rooms_open_posts_the_room(monkeypatch, tmp_path):
    charter = tmp_path / "charter.txt"
    charter.write_text("A committee.", encoding="utf-8")
    seats = tmp_path / "seats.json"
    seats.write_text(json.dumps([
        {"peer": "lab-ovh", "role": "chair", "family": "orch"},
        {"peer": "drop", "role": "seat", "family": "claude"},
    ]), encoding="utf-8")
    seen = _spy(monkeypatch, "post_json", payload={"channel": "fusion-aid"})
    rc = cm.run_committee([
        "rooms", "open", "--name", "fusion-aid", "--charter-file", str(charter),
        "--card-id", "1319", "--seats-file", str(seats)])
    assert rc == 0
    assert seen["url"].endswith("/committee/rooms")
    assert seen["body"]["name"] == "fusion-aid"
    assert seen["body"]["charter"] == "A committee."
    assert seen["body"]["card_id"] == 1319
    assert seen["body"]["seats"][0]["role"] == "chair"


def test_baseline_absent_discovers_the_motion_and_posts(monkeypatch):
    _spy(monkeypatch, "get_json", payload=_open_motion_payload())
    seen = _spy(monkeypatch, "post_json", payload={"motion_id": 7})
    rc = cm.run_committee(["baseline-absent", "fusion-aid", "--reason", "absent"])
    assert rc == 0
    assert seen["url"].endswith("/committee/fusion-aid/motions/7/baseline-absent")
    assert seen["body"] == {"reason": "absent"}


def test_recast_posts(monkeypatch):
    seen = _spy(monkeypatch, "post_json", payload={"voided_peer": "drop", "seated": "new"})
    rc = cm.run_committee([
        "recast", "fusion-aid", "--peer", "drop", "--new-peer", "new",
        "--family", "claude", "--reason", "drop is out"])
    assert rc == 0
    assert seen["url"].endswith("/committee/fusion-aid/recast")
    assert seen["body"]["new_peer"] == "new"
