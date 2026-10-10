"""card #1068 — structured accept, CLI half: --accept-json / --evidence-json
(ruling 6: keep --accept/--evidence, add the -json flags; no per-condition
flags). The gateway decides what a row needs; the CLI parses JSON locally,
refuses what it can see, and propagates the gateway's refusals verbatim."""
import json

import pytest

from swarph_cli.commands import board

PASS2 = {"pass": [{"id": "c1", "text": "the widget sprockets"},
                  {"id": "c2", "text": "the log names the count"}],
         "fail": ["no sprockets observed"]}


def test_ask_payload_sends_accept_json_and_omits_accept():
    p = board._ask_payload("lab-ovh", "wire it", holder="me",
                           accept_json=PASS2)
    assert p["accept_json"] == PASS2
    assert "accept" not in p


def test_ask_payload_refuses_both_accept_forms():
    with pytest.raises(ValueError):
        board._ask_payload("lab-ovh", "wire it", accept="PASS = x | FAIL = y",
                           accept_json=PASS2)


def test_amend_payload_sends_accept_json():
    p = board._amend_payload(accept_json=PASS2)
    assert p == {"accept_json": PASS2}


def test_amend_payload_refuses_both_accept_forms():
    with pytest.raises(ValueError):
        board._amend_payload(accept="PASS = x | FAIL = y",
                             accept_json=PASS2)


def test_close_payload_sends_map_without_string():
    p = board._close_payload("pass", None, {"c1": "a", "c2": "b"})
    assert p == {"outcome": "pass", "evidence_json": {"c1": "a", "c2": "b"}}


def test_close_payload_refuses_neither_evidence():
    with pytest.raises(ValueError):
        board._close_payload("pass", None, None)


def test_json_flag_rejects_invalid_json():
    with pytest.raises(ValueError):
        board._json_flag("--evidence-json", "{nope")


def test_close_parser_evidence_optional_with_json_flag():
    parser = board._build_parser()
    args = parser.parse_args(["obligations", "close", "5", "--outcome", "pass",
                              "--evidence-json", '{"c1": "a"}'])
    assert args.evidence is None
    assert json.loads(args.evidence_json) == {"c1": "a"}


def test_ask_parser_has_accept_json_flag():
    parser = board._build_parser()
    args = parser.parse_args(["cards", "ask", "20", "wire the thing",
                              "--holder", "me",
                              "--accept-json", '{"pass": []}'])
    assert json.loads(args.accept_json) == {"pass": []}


def test_accept_line_renders_json_conditions_not_blob():
    out = board._accept_line(json.dumps(PASS2))
    assert "c1" in out and "the widget sprockets" in out
    assert "{" not in out and "no sprockets observed" in out


def test_accept_line_prose_unchanged():
    assert board._accept_line(None).startswith("NO ACCEPT CHECK")
    assert "NO FAIL BRANCH" in board._accept_line("verify it works")
    assert board._accept_line("PASS = x | FAIL = y") == "accept: PASS = x | FAIL = y"


def test_format_obligations_marks_json_conditions():
    out = board._format_obligations({
        "as_of": "t", "obligations": [
            {"id": 1, "card_id": 2, "holder": "h", "status": "open",
             "kind": "action", "overdue": False,
             "accept_state": "fail-branch-detected",
             "unclosable_reason": None, "accept_format": "json",
             "conditions": [{"id": "c1", "text": "t", "evidence": "e"},
                            {"id": "c2", "text": "t2", "evidence": None}]}]})
    assert "conds:1/2" in out


def test_format_obligations_skips_conds_for_prose():
    out = board._format_obligations({
        "as_of": "t", "obligations": [
            {"id": 1, "card_id": 2, "holder": "h", "status": "closed",
             "kind": "action", "overdue": False,
             "accept_state": "fail-branch-detected",
             "unclosable_reason": None, "close_outcome": "pass",
             "accept_format": "prose",
             "conditions": [{"id": "all", "text": "t", "evidence": "e"}]}]})
    assert "conds:" not in out
