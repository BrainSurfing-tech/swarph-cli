"""Proxy route fixtures from gateway 5a567e1/server.py:4639,16619,6472."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from swarph_cli.proxy_routes import ProxyError, route_for


def test_fixed_card_read_pins_actor_and_rejects_raw_route_controls():
    assert route_for("board.card.get", {"id": 20}, "cell-one") == (
        "GET", "/board/cards/20?actor=cell-one", None)
    for extra in ("path", "method", "headers", "actor", "from_node", "cc", "obligation_id"):
        with pytest.raises(ProxyError, match="bad_args"):
            route_for("board.card.get", {"id": 20, extra: "/peers/register"}, "cell-one")
    with pytest.raises(ProxyError, match="verb_not_allowed"):
        route_for("/peers/register", {}, "cell-one")


@pytest.mark.parametrize("verb,args,expected", [
    # Gateway server.py:4639,4698,21879,16638,17549,7360,12103.
    ("whoami", {}, ("GET", "/whoami", None)),
    ("mesh.peers", {"enabled_only": False}, ("GET", "/peers?enabled_only=false", None)),
    ("rights", {}, ("GET", "/peers/cell-one/grants", None)),
    ("board.cards.thread", {"id": 20, "limit": 5},
     ("GET", "/board/cards/20/thread?actor=cell-one&limit=5", None)),
    ("board.obligations.list", {"id": 7, "holder": "SELF", "overdue": True},
     ("GET", "/board/obligations?actor=cell-one&obligation_id=7&holder=SELF&overdue=true", None)),
    ("mesh.inbox", {"limit": 3, "since": "2026-11-09T12:00:00+00:00"},
     ("GET", "/messages?to_node=cell-one&limit=3&since=2026-11-09T12%3A00%3A00%2B00%3A00", None)),
    ("memory.get", {"slug": "reference_auth"},
     ("POST", "/memory", {"op": "get", "arguments": {"slug": "reference_auth"}})),
    ("memory.list", {"type": "reference", "tag": "auth", "limit": 5},
     ("POST", "/memory", {"op": "list", "arguments": {"type": "reference", "tag": "auth", "limit": 5}})),
])
def test_closed_read_routes_match_gateway_fixtures(verb, args, expected):
    assert route_for(verb, args, "cell-one") == expected


@pytest.mark.parametrize("verb,args,expected", [
    # Gateway server.py:6472,6508,7618,7622,17847,17851,18504,20642.
    ("mesh.send", {"to_node": "peer-two", "content": "hello", "reply_to": 9},
     ("POST", "/messages", {"from_node": "cell-one", "to_node": "peer-two", "content": "hello", "kind": "status", "reply_to": 9})),
    ("channel.post", {"channel": "releases", "content": "shipped", "kind": "fyi"},
     ("POST", "/messages", {"from_node": "cell-one", "channel": "releases", "content": "shipped", "kind": "fyi"})),
    ("board.cards.say", {"id": 20, "content": "update"},
     ("POST", "/messages", {"from_node": "cell-one", "content": "update", "kind": "status"})),
    ("board.cards.say", {"id": 20, "content": "update", "to_node": "peer-two"},
     ("POST", "/messages", {"from_node": "cell-one", "to_node": "peer-two", "content": "update", "kind": "status"})),
    ("mesh.mark_read", {"ids": [3, 9]}, ("POST", "/messages/read", {"ids": [3, 9]})),
    ("board.obligations.take", {"id": 7, "expect_status": "open"},
     ("POST", "/board/obligations/7/take", {"expect_status": "open"})),
    ("board.obligations.decline", {"id": 7, "expect_status": "open", "why": "not my lane"},
     ("POST", "/board/obligations/7/decline", {"expect_status": "open", "why": "not my lane"})),
    ("board.obligations.close", {"id": 7, "expect_status": "open", "expect_accept_sha256": "a" * 64, "outcome": "pass", "evidence": "tests passed"},
     ("POST", "/board/obligations/7/close", {"expect_status": "open", "expect_accept_sha256": "a" * 64, "outcome": "pass", "evidence": "tests passed"})),
    ("board.cards.move", {"id": 20, "stage": "build", "expect_stage": "plan", "expect_stage_version": 0},
     ("PATCH", "/board/cards/20", {"actor": "cell-one", "stage": "build", "expect_stage": "plan", "expect_stage_version": 0})),
])
def test_closed_write_routes_pin_sender_and_required_guards(verb, args, expected):
    assert route_for(verb, args, "cell-one") == expected


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "20", None, {}, [], "../peers/register", "20?actor=commander"])
def test_card_ids_are_positive_integers_without_coercion(value):
    with pytest.raises(ProxyError, match="bad_args"):
        route_for("board.card.get", {"id": value}, "cell-one")


@pytest.mark.parametrize("verb,args", [
    ("whoami", {}), ("board.card.get", {"id": 20}),
    ("board.cards.thread", {"id": 20}), ("board.obligations.list", {}),
    ("mesh.peers", {}), ("rights", {}), ("mesh.inbox", {}),
    ("memory.get", {"slug": "reference_auth"}), ("memory.list", {}),
    ("mesh.send", {"to_node": "peer-two", "content": "hello"}),
    ("channel.post", {"channel": "releases", "content": "hello"}),
    ("board.cards.say", {"id": 20, "content": "hello"}),
    ("mesh.mark_read", {"ids": [1]}),
    ("board.obligations.take", {"id": 7, "expect_status": "open"}),
    ("board.obligations.decline", {"id": 7, "expect_status": "open", "why": "no"}),
    ("board.obligations.close", {"id": 7, "expect_status": "open", "expect_accept_sha256": "a" * 64, "outcome": "pass", "evidence": "yes"}),
    ("board.cards.move", {"id": 20, "stage": "build", "expect_stage": "plan", "expect_stage_version": 0}),
])
def test_every_verb_is_closed_to_actor_routes_and_forbidden_fields(verb, args):
    for extra in ("path", "method", "headers", "actor", "from_node", "cc", "obligation_id", "thread_id", "token"):
        with pytest.raises(ProxyError, match="bad_args") as caught:
            route_for(verb, {**args, extra: "SECRET_SENTINEL"}, "cell-one")
        assert str(caught.value) == "bad_args"
        assert caught.value.error == "bad_args"


@pytest.mark.parametrize("verb,args", [
    ("mesh.inbox", {"to_node": "peer-two"}), ("rights", {"peer": "peer-two"}),
    ("memory.get", {"slug": "../token"}), ("memory.get", {"slug": "good", "op": "put"}),
    ("memory.list", {"arguments": {"tool": "put_page"}}),
    ("memory.list", {"limit": True}), ("mesh.inbox", {"unread_only": "false"}),
    ("mesh.peers", {"enabled_only": 1}), ("mesh.inbox", {"since": "now&to_node=peer-two"}),
    ("board.cards.thread", {"id": 20, "limit": 1001}),
    ("board.obligations.list", {"status": "taken"}),
    ("board.obligations.list", {"holder": "peer&actor=commander"}),
    ("mesh.send", {"to_node": "../peers/register", "content": "hello"}),
    ("mesh.send", {"to_node": "peer-two", "content": "hello", "reply_to": True}),
    ("channel.post", {"channel": "a?b", "content": "hello"}),
    ("board.cards.say", {"id": 20, "content": " "}),
    ("board.cards.say", {"id": 20, "content": "hi", "kind": "admin"}),
    ("mesh.mark_read", {"ids": []}), ("mesh.mark_read", {"ids": [True]}),
    ("mesh.mark_read", {"ids": [1] * 501}),
    ("board.obligations.take", {"id": 7}),
    ("board.obligations.decline", {"id": 7, "why": "no"}),
    ("board.obligations.close", {"id": 7, "expect_status": "open", "outcome": "pass", "evidence": "yes"}),
    ("board.obligations.close", {"id": 7, "expect_accept_sha256": "a" * 64, "outcome": "pass", "evidence": "yes"}),
    ("board.obligations.close", {"id": 7, "expect_status": "open", "expect_accept_sha256": "wrong", "outcome": "pass", "evidence": "yes"}),
    ("board.cards.move", {"id": 20, "stage": "build", "expect_stage": "plan"}),
    ("board.cards.move", {"id": 20, "stage": "build", "expect_stage_version": 0}),
    ("board.cards.move", {"id": 20, "stage": "build", "expect_stage": "plan", "expect_stage_version": True}),
    ("board.cards.move", {"id": 20, "stage": "build", "expect_stage": "plan", "expect_stage_version": -1}),
])
def test_bad_typed_arguments_and_missing_guards_are_refused(verb, args):
    with pytest.raises(ProxyError, match="bad_args"):
        route_for(verb, args, "cell-one")


@pytest.mark.parametrize("verb", ["board.obligations.release", "memory.links", "board.cards.create", "grants", "peers.register", "token.mint", "POST /peers/register", None, [], {}])
def test_any_unlisted_verb_is_refused(verb):
    with pytest.raises(ProxyError, match="verb_not_allowed"):
        route_for(verb, {}, "cell-one")


def test_done_is_refused_even_with_complete_guards():
    with pytest.raises(ProxyError, match="verb_not_allowed"):
        route_for("board.cards.move", {"id": 20, "stage": "done", "expect_stage": "ready", "expect_stage_version": 1}, "cell-one")


@pytest.mark.parametrize("args", [None, [], "args", True])
def test_args_must_be_an_object(args):
    with pytest.raises(ProxyError, match="bad_args"):
        route_for("whoami", args, "cell-one")


def test_query_values_are_encoded_and_invalid_config_cell_is_rejected():
    assert route_for("mesh.inbox", {"since": "2026-11-09T12:00:00+00:00"}, "cell-one")[1] == (
        "/messages?to_node=cell-one&since=2026-11-09T12%3A00%3A00%2B00%3A00")
    with pytest.raises(ProxyError, match="bad_args"):
        route_for("rights", {}, "../peers/register")


def test_route_result_does_not_retain_mutable_input_containers():
    args = {"ids": [1, 2]}
    body = route_for("mesh.mark_read", args, "cell-one")[2]
    args["ids"].append(3)
    assert body == {"ids": [1, 2]}


# Independent wire fixtures: mesh-gateway@5a567e1aea4f1bcb49c31e59e16fde11d6d8b5d0.
# server.py:16619/15936 card GET, :16707 thread, :17676 obligations,
# :4698/:4728 peers, :21912 rights, :7550 inbox, :12103 memory,
# :6953 DM, :7213 channel, :7661 mark-read, :18026 take, :18110 decline,
# :20623 close, :21394 PATCH card. No production route code builds these fixtures.
_WIRE_THREAD = "12345678-1234-4321-8123-123456789abc"
_WIRE_CARD = {"id": 20, "project_id": 1, "title": "proxy fixture", "stage": "plan",
              "stage_version": 0, "assignee": "peer-two", "thread_uuid": _WIRE_THREAD,
              "tasks": [], "open_obligations": []}
_WIRE_OBLIGATIONS = {"obligations": [], "as_of": "2026-11-09T12:00:00+00:00", "truncated": False}
_WIRE_DM = {"id": 91, "from_node": "cell-one", "to_node": "peer-two", "kind": "status",
            "thread_id": None, "created_at": "2026-11-09T12:00:00Z", "closed_obligations": [],
            "open_obligations": [], "obligation_check": "no_thread", "minted_by": None,
            "minted_key_generation": None}
_WIRE_SAY = {**_WIRE_DM, "thread_id": _WIRE_THREAD, "obligation_check": "checked"}
_WIRE_CHANNEL = {"id": 92, "from_node": "cell-one", "channel": "releases", "kind": "fyi",
                 "created_at": "2026-11-09T12:00:00Z", "mentions": [], "minted_by": None,
                 "minted_key_generation": None, "closed_obligations": [], "open_obligations": [],
                 "obligation_check": "not_applicable"}


@pytest.fixture
def wire_gateway(tmp_path):
    """A real HTTP boundary with a closed independent gateway route registry."""
    if os.name != "posix":
        pytest.skip("the credential-holding proxy is Unix-only; pure routing still runs")
    from swarph_cli.local_proxy import Config, Proxy

    routes = {
        ("GET", "/whoami"): {"regime": "per_peer_token", "peer": "cell-one", "key_generation": 1,
                             "kind": None, "is_root": False, "is_operator": False, "node": None,
                             "role": None, "scopes_note": "fixture credential"},
        ("GET", "/board/cards/20?actor=cell-one"): dict(_WIRE_CARD),
        ("GET", "/board/cards/20/thread?actor=cell-one&limit=5"):
            {"card_id": 20, "thread_uuid": _WIRE_THREAD, "messages": []},
        ("GET", "/board/obligations?actor=cell-one&obligation_id=7&holder=SELF&overdue=true"):
            dict(_WIRE_OBLIGATIONS),
        ("GET", "/peers?enabled_only=false"): {"peers": []},
        ("GET", "/peers/cell-one/grants"): {"peer": "cell-one", "groups": [], "grants": []},
        ("GET", "/messages?to_node=cell-one&unread_only=true&limit=3"):
            {"messages": [], "n": 0},
        ("POST", "/memory"): {"result": {"slug": "reference_auth", "body": "fixture page"}},
        ("POST", "/messages"): dict(_WIRE_DM),
        ("POST", "/messages/read"): {"read_at": "2026-11-09T12:00:00Z", "ids": [3, 9], "skipped": []},
        ("POST", "/board/obligations/7/take"):
            {"id": 7, "card_id": 20, "holder": "cell-one", "taken_at": "2026-11-09T12:00:00Z",
             "step": "build", "state": "open", "due": None,
             "close_with": "swarph board obligations close 7 --outcome pass --evidence <container>"},
        ("POST", "/board/obligations/7/decline"):
            {"id": 7, "declined_by": "cell-one", "remaining_eligible": ["peer-two"],
             "state": "requested", "unstaffable": False},
        ("POST", "/board/obligations/7/close"):
            {"id": 7, "card_id": 20, "holder": "cell-one", "status": "closed", "thread_uuid": _WIRE_THREAD,
             "closed_by": "cell-one", "closed_at": "2026-11-09T12:00:00Z", "close_outcome": "pass",
             "close_evidence": "tests passed", "verdict": None, "remaining_open_obligations": [],
             "move_ready_derived": False, "usage": None},
        ("PATCH", "/board/cards/20"): {**_WIRE_CARD, "stage": "build", "stage_version": 1},
    }
    seen, authenticated = [], []
    fixture_auth = "fixture-wire-peer-credential"

    class Gateway(BaseHTTPRequestHandler):
        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else None
            seen.append((self.command, self.path, body))
            authenticated.append(self.headers.get("Authorization") == "Bearer " + fixture_auth)
            response = routes.get((self.command, self.path))
            status = 200 if response is not None and authenticated[-1] else 404
            if (self.command, self.path) == ("POST", "/memory") and body == {
                    "op": "list", "arguments": {"type": "reference", "tag": "auth", "limit": 5}}:
                response = {"result": [{"slug": "reference_auth", "type": "reference", "tags": ["auth"]}]}
            if (self.command, self.path) == ("POST", "/messages") and body.get("channel"):
                response = dict(_WIRE_CHANNEL)
            elif (self.command, self.path) == ("POST", "/messages") and body.get("thread_id") == _WIRE_THREAD:
                response = dict(_WIRE_SAY)
            payload = json.dumps(response if status == 200 else {"detail": "fixture route rejected"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = respond

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    config = Config("cell-one", os.getuid() + 77777, f"http://127.0.0.1:{server.server_port}",
                    tmp_path / "unused-token", tmp_path / "unused.sock", tmp_path / "audit.jsonl")
    try:
        yield Proxy(config, fixture_auth), seen, authenticated, routes
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("verb,args,expected_wire,expected_body", [
    ("whoami", {}, [("GET", "/whoami", None)],
     {"regime": "per_peer_token", "peer": "cell-one", "key_generation": 1, "kind": None,
      "is_root": False, "is_operator": False, "node": None, "role": None, "scopes_note": "fixture credential"}),
    ("board.card.get", {"id": 20}, [("GET", "/board/cards/20?actor=cell-one", None)], _WIRE_CARD),
    ("board.cards.thread", {"id": 20, "limit": 5},
     [("GET", "/board/cards/20/thread?actor=cell-one&limit=5", None)],
     {"card_id": 20, "thread_uuid": _WIRE_THREAD, "messages": []}),
    ("board.obligations.list", {"id": 7, "holder": "SELF", "overdue": True},
     [("GET", "/board/obligations?actor=cell-one&obligation_id=7&holder=SELF&overdue=true", None)], _WIRE_OBLIGATIONS),
    ("mesh.peers", {"enabled_only": False}, [("GET", "/peers?enabled_only=false", None)], {"peers": []}),
    ("rights", {}, [("GET", "/peers/cell-one/grants", None)], {"peer": "cell-one", "groups": [], "grants": []}),
    ("mesh.inbox", {"unread_only": True, "limit": 3},
     [("GET", "/messages?to_node=cell-one&unread_only=true&limit=3", None)], {"messages": [], "n": 0}),
    ("memory.get", {"slug": "reference_auth"},
     [("POST", "/memory", {"op": "get", "arguments": {"slug": "reference_auth"}})],
     {"result": {"slug": "reference_auth", "body": "fixture page"}}),
    ("memory.list", {"type": "reference", "tag": "auth", "limit": 5},
     [("POST", "/memory", {"op": "list", "arguments": {"type": "reference", "tag": "auth", "limit": 5}})],
     {"result": [{"slug": "reference_auth", "type": "reference", "tags": ["auth"]}]}),
    ("board.cards.say", {"id": 20, "content": "update"},
     [("GET", "/board/cards/20?actor=cell-one", None),
      ("POST", "/messages", {"from_node": "cell-one", "to_node": "peer-two", "thread_id": _WIRE_THREAD,
                             "content": "update", "kind": "status"})], _WIRE_SAY),
    ("mesh.send", {"to_node": "peer-two", "content": "hello", "reply_to": 9},
     [("POST", "/messages", {"from_node": "cell-one", "to_node": "peer-two", "content": "hello",
                             "kind": "status", "reply_to": 9})], _WIRE_DM),
    ("channel.post", {"channel": "releases", "content": "shipped", "kind": "fyi"},
     [("POST", "/messages", {"from_node": "cell-one", "channel": "releases", "content": "shipped", "kind": "fyi"})],
     _WIRE_CHANNEL),
    ("mesh.mark_read", {"ids": [3, 9]}, [("POST", "/messages/read", {"ids": [3, 9]})],
     {"read_at": "2026-11-09T12:00:00Z", "ids": [3, 9], "skipped": []}),
    ("board.obligations.take", {"id": 7, "expect_status": "open"},
     [("POST", "/board/obligations/7/take", {"expect_status": "open"})],
     {"id": 7, "card_id": 20, "holder": "cell-one", "taken_at": "2026-11-09T12:00:00Z", "step": "build",
      "state": "open", "due": None, "close_with": "swarph board obligations close 7 --outcome pass --evidence <container>"}),
    ("board.obligations.decline", {"id": 7, "expect_status": "open", "why": "not my lane"},
     [("POST", "/board/obligations/7/decline", {"expect_status": "open", "why": "not my lane"})],
     {"id": 7, "declined_by": "cell-one", "remaining_eligible": ["peer-two"], "state": "requested", "unstaffable": False}),
    ("board.obligations.close", {"id": 7, "expect_status": "open", "expect_accept_sha256": "a" * 64,
                                 "outcome": "pass", "evidence": "tests passed"},
     [("POST", "/board/obligations/7/close", {"expect_status": "open", "expect_accept_sha256": "a" * 64,
                                             "outcome": "pass", "evidence": "tests passed"})],
     {"id": 7, "card_id": 20, "holder": "cell-one", "status": "closed", "thread_uuid": _WIRE_THREAD,
      "closed_by": "cell-one", "closed_at": "2026-11-09T12:00:00Z", "close_outcome": "pass", "close_evidence": "tests passed",
      "verdict": None, "remaining_open_obligations": [], "move_ready_derived": False, "usage": None}),
    ("board.cards.move", {"id": 20, "stage": "build", "expect_stage": "plan", "expect_stage_version": 0},
     [("GET", "/board/cards/20?actor=cell-one", None),
      ("PATCH", "/board/cards/20", {"actor": "cell-one", "stage": "build", "expect_stage": "plan", "expect_stage_version": 0})],
     {**_WIRE_CARD, "stage": "build", "stage_version": 1}),
])
def test_every_verb_round_trips_through_real_http(wire_gateway, verb, args, expected_wire, expected_body):
    proxy, seen, authenticated, _ = wire_gateway
    request_id = "97fb3490-9147-4da6-acb2-29bac3df210c"
    result = proxy.request({"request_id": request_id, "verb": verb, "args": args})
    assert result == {"request_id": request_id, "ok": True, "status": 200, "body": expected_body}
    assert seen == expected_wire
    assert authenticated and all(authenticated)


@pytest.mark.parametrize("assignee,explicit,recipient", [
    ("peer-two", "Explicit.Peer_3", "Explicit.Peer_3"), (None, None, "cell-one"),
    ("Assigned.Peer_4", None, "Assigned.Peer_4"),
])
def test_say_derives_thread_and_recipient_over_http(wire_gateway, assignee, explicit, recipient):
    proxy, seen, authenticated, routes = wire_gateway
    routes[("GET", "/board/cards/20?actor=cell-one")]["assignee"] = assignee
    args = {"id": 20, "content": "update"}
    if explicit:
        args["to_node"] = explicit
    result = proxy.request({"request_id": "97fb3490-9147-4da6-acb2-29bac3df210c", "verb": "board.cards.say", "args": args})
    assert result["ok"] is True
    assert seen == [("GET", "/board/cards/20?actor=cell-one", None),
                    ("POST", "/messages", {"from_node": "cell-one", "to_node": recipient,
                                           "thread_id": _WIRE_THREAD, "content": "update", "kind": "status"})]
    assert all(authenticated)


@pytest.mark.parametrize("version", [None, True, -1, "0"])
def test_move_fails_closed_before_patch_on_old_gateway(wire_gateway, version):
    proxy, seen, _, routes = wire_gateway
    routes[("GET", "/board/cards/20?actor=cell-one")]["stage_version"] = version
    result = proxy.request({"request_id": "97fb3490-9147-4da6-acb2-29bac3df210c", "verb": "board.cards.move",
                            "args": {"id": 20, "stage": "build", "expect_stage": "plan", "expect_stage_version": 0}})
    assert result["error"] == "verb_not_allowed"
    assert seen == [("GET", "/board/cards/20?actor=cell-one", None)]


@pytest.mark.parametrize("verb,args,error", [
    ("/peers/register", {}, "verb_not_allowed"),
    ("whoami", {"path": "/peers/register"}, "bad_args"),
    ("board.cards.say", {"id": 20, "content": "update", "thread_id": _WIRE_THREAD}, "bad_args"),
    ("mesh.send", {"to_node": "peer-two", "content": "hello", "from_node": "commander"}, "bad_args"),
    ("mesh.inbox", {"to_node": "peer-two"}, "bad_args"),
    ("board.cards.move", {"id": 20, "stage": "done", "expect_stage": "ready", "expect_stage_version": 1}, "verb_not_allowed"),
])
def test_forbidden_routes_never_reach_http_gateway(wire_gateway, verb, args, error):
    proxy, seen, authenticated, _ = wire_gateway
    result = proxy.request({"request_id": "97fb3490-9147-4da6-acb2-29bac3df210c", "verb": verb, "args": args})
    assert seen == []
    assert authenticated == []
    assert result["error"] == error
