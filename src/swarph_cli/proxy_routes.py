"""Closed route construction for the token-isolating cell proxy."""
import json
import re
from datetime import datetime
from urllib.parse import urlencode

# Memory reads use POST; HTTP method is not a capability boundary.
READ_VERBS = frozenset({
    "whoami", "board.projects.list", "board.cards.list", "board.card.get",
    "board.cards.graph", "board.cards.thread", "board.obligations.list",
    "mesh.peers", "rights", "mesh.inbox", "memory.get", "memory.list",
    "committee.motions.list", "committee.motion.get",
})


class ProxyError(Exception):
    def __init__(self, error: str):
        self.error = error
        super().__init__(error)


def _name(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is not None


def _id(value):
    return type(value) is int and value > 0


def _committee_channel(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]{1,64}", value) is not None


def _limit(value):
    return _id(value) and value <= 1000


def _bool(value):
    return type(value) is bool


def _text(maximum):
    return lambda v: isinstance(v, str) and 1 <= len(v) <= maximum and bool(v.strip())


def _kind(value):
    return value in ("status", "question", "answer", "unblock", "fyi")


def _stage(value):
    return value in ("proposed", "idea", "spec", "plan", "build", "review", "ready", "done", "parked")


def _status(value):
    return value in ("open", "closed", "fallback_fired", "withdrawn")


def _since(value):
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _evidence_map(value):
    # Gateway 657e7e9/server.py:13963; condition-id membership stays server-side.
    return (type(value) is dict
            and all(isinstance(k, str) and isinstance(v, str) and v.strip()
                    and len(v) <= 16000 for k, v in value.items())
            and len(json.dumps(value, sort_keys=True)) <= 64000)


_SCHEMAS = {
    "whoami": ({}, {}),
    "committee.motions.list": ({"channel": _committee_channel}, {}),
    "committee.motion.get": ({"channel": _committee_channel, "id": _id}, {}),
    "board.projects.list": ({}, {}),
    "board.cards.list": ({}, {"project": _id, "stage": _stage, "assignee": _name,
                             "label": _name, "q": _text(1000)}),
    "board.card.get": ({"id": _id}, {}),
    "board.cards.graph": ({"id": _id}, {}),
    "board.cards.thread": ({"id": _id}, {"limit": _limit}),
    "board.obligations.list": ({}, {
        "id": _id, "card_id": _id, "status": _status, "holder": _name,
        "created_by": _name, "limit": _limit, "overdue": _bool, "grandfathered": _bool}),
    "mesh.peers": ({}, {"enabled_only": _bool}),
    "rights": ({}, {}),
    "mesh.inbox": ({}, {"limit": _limit, "unread_only": _bool, "since": _since, "kind": _kind}),
    "memory.get": ({"slug": lambda v: isinstance(v, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", v) is not None}, {}),
    "memory.list": ({}, {"type": _name, "tag": _name, "limit": _limit}),
    "board.cards.create": ({"project": _id, "title": _text(1000)}, {
        "body": _text(200_000), "priority": lambda v: type(v) is int and 0 <= v <= 13,
        "labels": lambda v: type(v) is list and 1 <= len(v) <= 12 and all(
            isinstance(i, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}[a-z0-9]", i.lower()) is not None for i in v),
        "assignee": _name}),
    "board.cards.say": ({"id": _id, "content": _text(200_000)}, {"kind": _kind, "to_node": _name}),
    "mesh.send": ({"to_node": _name, "content": _text(200_000)}, {"kind": _kind, "reply_to": _id}),
    "channel.post": ({"channel": _name, "content": _text(200_000)}, {"kind": _kind}),
    "mesh.mark_read": ({"ids": lambda v: type(v) is list and 1 <= len(v) <= 500 and all(_id(i) for i in v)}, {}),
    "board.obligations.take": ({"id": _id, "expect_status": _status}, {}),
    "board.obligations.decline": ({"id": _id, "expect_status": _status, "why": _text(2000)}, {}),
    "board.obligations.close": ({
        "id": _id, "expect_status": _status,
        "expect_accept_sha256": lambda v: isinstance(v, str) and re.fullmatch(r"[a-fA-F0-9]{64}", v) is not None,
        "outcome": lambda v: v in ("pass", "fail", "cannot_evaluate", "skipped")},
        {"evidence": _text(4000), "evidence_json": _evidence_map}),
    "board.cards.move": ({"id": _id, "stage": _stage, "expect_stage": _stage,
                          "expect_stage_version": lambda v: type(v) is int and v >= 0}, {}),
}


def _query(path, params):
    encoded = urlencode({k: str(v).lower() if type(v) is bool else v for k, v in params.items()})
    return path + ("?" + encoded if encoded else "")


def route_for(verb: str, args: dict, cell: str) -> tuple[str, str, dict | None]:
    if not isinstance(verb, str) or verb not in _SCHEMAS:
        raise ProxyError("verb_not_allowed")
    required, optional = _SCHEMAS[verb]
    allowed = required | optional
    if (type(args) is not dict or not required.keys() <= args.keys()
            or not args.keys() <= allowed.keys() or not _name(cell)
            or any(not allowed[key](value) for key, value in args.items())):
        raise ProxyError("bad_args")
    if verb == "board.obligations.close" and not {"evidence", "evidence_json"} & args.keys():
        raise ProxyError("bad_args")
    if verb == "board.cards.create":
        return "POST", "/board/cards", {
            "project_id": args["project"],
            **{k: list(v) if k == "labels" else v for k, v in args.items() if k != "project"}, "actor": cell}
    if verb in ("committee.motions.list", "committee.motion.get"):
        suffix = f"/{args['id']}" if verb == "committee.motion.get" else ""
        return "GET", f"/committee/{args['channel']}/motions" + suffix, None
    if verb == "board.cards.move":
        if args["stage"] == "done":
            raise ProxyError("verb_not_allowed")
        return "PATCH", f"/board/cards/{args['id']}", {"actor": cell, **{k: v for k, v in args.items() if k != "id"}}
    if verb in ("mesh.send", "channel.post", "board.cards.say"):
        return "POST", "/messages", {"from_node": cell, "kind": args.get("kind", "status"),
                                     **{k: v for k, v in args.items() if k != "id"}}
    if verb == "mesh.mark_read":
        return "POST", "/messages/read", {"ids": list(args["ids"])}
    if verb.startswith("board.obligations.") and verb != "board.obligations.list":
        return "POST", f"/board/obligations/{args['id']}/{verb.rsplit('.', 1)[1]}", {
            k: dict(v) if k == "evidence_json" else v for k, v in args.items() if k != "id"}
    if verb in ("board.card.get", "board.cards.thread", "board.cards.graph"):
        suffix = {"board.cards.thread": "/thread", "board.cards.graph": "/graph"}.get(verb, "")
        params = {"actor": cell, **{k: v for k, v in args.items() if k != "id"}}
        return "GET", _query(f"/board/cards/{args['id']}" + suffix, params), None
    if verb in ("board.projects.list", "board.cards.list"):
        path = "/board/projects" if verb == "board.projects.list" else "/board/cards"
        return "GET", _query(path, {"actor": cell, **args}), None
    if verb == "board.obligations.list":
        params = {"actor": cell, **{("obligation_id" if k == "id" else k): v for k, v in args.items()}}
        return "GET", _query("/board/obligations", params), None
    if verb.startswith("memory."):
        return "POST", "/memory", {"op": verb.split(".")[1], "arguments": dict(args)}
    if verb == "mesh.inbox":
        return "GET", _query("/messages", {"to_node": cell, **args}), None
    if verb == "rights":
        return "GET", f"/peers/{cell}/grants", None
    return "GET", _query("/whoami" if verb == "whoami" else "/peers", args), None
