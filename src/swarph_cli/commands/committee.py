"""``swarph committee`` — client for the gateway's committee routes (card #1323).

A seat posts a sealed position with ``post``; the motion id is discovered from
the room via ``motion <channel>`` (a GET list, added in the card-#1323 gateway
PR) so a harness that blocks raw HTTP can still ballot. Chair-only verbs:
``rooms open``, ``motion <channel> --open``, ``recast``, ``baseline-absent``.
A live seat or the chair reads the room charter with ``rooms show <channel>``.
That command prints ``committee_rooms.charter`` verbatim and nothing else.
Uses the shared token resolver and NEVER prints the token.

Card #1516 adds ``results <channel> [--motion ID] --json`` and ``--json`` on
``rooms open`` / ``motion`` / ``post``. ``results`` prints the gateway's motion
JSON exactly as returned: a ballot body appears ONLY where the gateway
returned one — the client never adds, infers, copies, or synthesizes a body
from any other field, so a sealed round stays body-less on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from ._gateway_client import (
    add_common_args,
    get_json,
    post_json,
    resolve_self_name,
    resolve_token,
)

ROUNDS = ("positions", "rebuttal", "vote")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph committee",
        description="Committee room client over the gateway's /committee routes.",
    )
    sub = p.add_subparsers(dest="command")

    motion = sub.add_parser(
        "motion", help="show the open motion, or (chair) open one with --open")
    motion.add_argument("channel", help="committee channel name")
    motion.add_argument("--open", action="store_true", help="open a motion (chair only)")
    motion.add_argument("--text", default=None, help="motion text (--open)")
    motion.add_argument("--options", default=None,
                        help="comma-separated options, at least two (--open)")
    motion.add_argument("--positions-deadline", default=None, help="ISO deadline")
    motion.add_argument("--rebuttal-deadline", default=None, help="ISO deadline")
    motion.add_argument("--vote-deadline", default=None, help="ISO deadline")
    motion.add_argument("--json", action="store_true",
                        help="print the gateway's response JSON on stdout")
    add_common_args(motion)

    post = sub.add_parser(
        "post", help="post a sealed ballot (round: positions|rebuttal|vote)")
    post.add_argument("channel", help="committee channel name")
    post.add_argument("--round", required=True, choices=ROUNDS)
    post.add_argument("--body-file", required=True, help="the ballot body, read verbatim")
    post.add_argument("--outcome", default=None, choices=("pass", "cannot_evaluate"),
                      help="sealed outcome (positions/vote only; default pass)")
    post.add_argument("--json", action="store_true",
                      help="print the gateway's response JSON on stdout")
    add_common_args(post)

    results = sub.add_parser(
        "results", help="print a motion's results JSON exactly as the gateway returns it")
    results.add_argument("channel", help="committee channel name")
    results.add_argument("--motion", default=None,
                         help="motion id (default: the open motion on the channel; "
                              "a closed motion needs --motion ID because the gateway "
                              "lists open motions only)")
    results.add_argument("--json", action="store_true",
                         help="print the gateway's response JSON on stdout (the "
                              "default output of this verb)")
    add_common_args(results)

    rooms = sub.add_parser("rooms", help="committee rooms")
    rsub = rooms.add_subparsers(dest="rooms_command")
    ropen = rsub.add_parser("open", help="open a room (chair only)")
    ropen.add_argument("--name", required=True, help="room name")
    ropen.add_argument("--charter-file", required=True, help="charter text, read verbatim")
    ropen.add_argument("--card-id", type=int, required=True)
    ropen.add_argument("--seats-file", required=True,
                       help="JSON list of {\"peer\", \"role\", \"family\"}")
    ropen.add_argument("--json", action="store_true",
                       help="print the gateway's response JSON on stdout")
    add_common_args(ropen)

    rshow = rsub.add_parser(
        "show", help="print the room charter verbatim (a live seat or the chair)")
    rshow.add_argument("channel", help="committee channel name")
    add_common_args(rshow)

    recast = sub.add_parser("recast", help="recast a seat (chair only)")
    recast.add_argument("channel", help="committee channel name")
    recast.add_argument("--peer", required=True)
    recast.add_argument("--new-peer", required=True)
    recast.add_argument("--family", required=True)
    recast.add_argument("--reason", required=True)
    recast.add_argument("--extend-seconds", type=int, default=None)
    add_common_args(recast)

    ba = sub.add_parser("baseline-absent", help="record the baseline ABSENT (chair only)")
    ba.add_argument("channel", help="committee channel name")
    ba.add_argument("--reason", required=True)
    add_common_args(ba)

    return p


def _fail(sub: str, status: int, payload) -> int:
    detail = payload.get("detail", "<error>") if isinstance(payload, dict) else "<error>"
    print(f"swarph committee {sub}: gateway {status}: {detail}", file=sys.stderr)
    return 1


def _ok(status: int) -> bool:
    return 200 <= status < 300


def _auth(args: argparse.Namespace) -> tuple[str, str]:
    self_name = resolve_self_name(args.self_name)
    token = resolve_token(self_name, args.token_file)
    return self_name, token


def _quote(channel: str) -> str:
    return urllib.parse.quote(channel, safe="")


def _read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc


def _print_json(payload) -> None:
    """Print the gateway payload exactly as returned (parseable JSON)."""
    print(json.dumps(payload))


def _latest_motion(motions: list) -> dict:
    """The latest motion in a list payload: highest id, else the last entry."""
    ids = [m.get("id") for m in motions if isinstance(m, dict)]
    if ids and all(isinstance(i, int) for i in ids):
        return max(motions, key=lambda m: m["id"])
    return motions[-1]


def _open_motion(base: str, token: str, channel: str, sub: str) -> dict:
    """The open motion on the channel, discovered from the list (never scanned)."""
    status, payload = get_json(f"{base}/committee/{_quote(channel)}/motions", token)
    if not _ok(status):
        raise RuntimeError(f"gateway {status}: "
                           f"{payload.get('detail') if isinstance(payload, dict) else payload}")
    motions = payload.get("motions", []) if isinstance(payload, dict) else []
    if not motions:
        raise RuntimeError(f"no open motion on channel {channel!r}")
    if len(motions) > 1:
        raise RuntimeError(f"{len(motions)} open motions on {channel!r}; expected at most one")
    return motions[0]


def _run_motion(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    if args.open:
        if not (args.text and args.options and args.positions_deadline
                and args.rebuttal_deadline and args.vote_deadline):
            print("swarph committee motion --open: --text, --options and the three "
                  "--*-deadline flags are required", file=sys.stderr)
            return 2
        options = [o.strip() for o in args.options.split(",") if o.strip()]
        body = {
            "text": args.text,
            "options": options,
            "positions_deadline": args.positions_deadline,
            "rebuttal_deadline": args.rebuttal_deadline,
            "vote_deadline": args.vote_deadline,
        }
        status, payload = post_json(f"{base}/committee/{_quote(args.channel)}/motions",
                                    body, token)
        if not _ok(status):
            return _fail("motion open", status, payload)
        if args.json:
            _print_json(payload)
            return 0
        print(f"opened motion {payload.get('id')} on {args.channel} "
              f"(phase {payload.get('phase')})")
        return 0
    status, payload = get_json(f"{base}/committee/{_quote(args.channel)}/motions", token)
    if not _ok(status):
        return _fail("motion", status, payload)
    if args.json:
        _print_json(payload)
        return 0
    motions = payload.get("motions", []) if isinstance(payload, dict) else []
    if not motions:
        print(f"no open motion on {args.channel}")
        return 0
    for m in motions:
        print(f"motion {m['id']} phase={m['phase']} on {m['channel']}")
        print(f"  positions_deadline={m['positions_deadline']}")
        print(f"  rebuttal_deadline={m['rebuttal_deadline']}")
        print(f"  vote_deadline={m['vote_deadline']}")
    return 0


def _run_post(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    text = _read_file(args.body_file)
    motion = _open_motion(base, token, args.channel, "post")
    body = {"round": args.round, "body": text}
    if args.round in ("positions", "vote"):
        body["outcome"] = args.outcome or "pass"
    status, payload = post_json(
        f"{base}/committee/{_quote(args.channel)}/motions/{motion['id']}/posts",
        body, token)
    if not _ok(status):
        return _fail("post", status, payload)
    if args.json:
        _print_json(payload)
        return 0
    print(f"posted {args.round} on motion {payload.get('motion_id')} "
          f"(phase {payload.get('phase')}, msg:{payload.get('message_id')})")
    return 0


def _run_results(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    if args.motion is not None:
        motion_id = args.motion
    else:
        status, payload = get_json(f"{base}/committee/{_quote(args.channel)}/motions", token)
        if not _ok(status):
            return _fail("results", status, payload)
        motions = payload.get("motions", []) if isinstance(payload, dict) else []
        if not motions:
            raise RuntimeError(
                f"no open motion on {args.channel}; a closed motion needs "
                f"--motion ID (the gateway lists open motions only)")
        motion_id = _latest_motion(motions)["id"]
    status, payload = get_json(
        f"{base}/committee/{_quote(args.channel)}/motions/{motion_id}", token)
    if not _ok(status):
        return _fail("results", status, payload)
    # SEAL RULE: print the gateway payload verbatim. A ballot body appears
    # only where the gateway returned one; nothing is filled in here.
    _print_json(payload)
    return 0


def _run_rooms_show(args: argparse.Namespace) -> int:
    """Print the room charter verbatim. Ballot fields on the payload are not printed."""
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    status, payload = get_json(
        f"{base}/committee/rooms/{_quote(args.channel)}", token)
    if not _ok(status):
        return _fail("rooms show", status, payload)
    charter = payload.get("charter") if isinstance(payload, dict) else None
    if not isinstance(charter, str):
        print("swarph committee rooms show: gateway response has no charter",
              file=sys.stderr)
        return 1
    sys.stdout.write(charter)
    return 0


def _run_rooms(args: argparse.Namespace) -> int:
    if args.rooms_command == "show":
        return _run_rooms_show(args)
    if args.rooms_command != "open":
        print("swarph committee rooms: unknown command; try `rooms open` or `rooms show`",
              file=sys.stderr)
        return 2
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    charter = _read_file(args.charter_file)
    seats = json.loads(_read_file(args.seats_file))
    body = {"name": args.name, "charter": charter, "card_id": args.card_id,
            "seats": seats}
    status, payload = post_json(f"{base}/committee/rooms", body, token)
    if not _ok(status):
        return _fail("rooms open", status, payload)
    if args.json:
        _print_json(payload)
        return 0
    print(f"opened committee room {payload.get('channel')}")
    return 0


def _run_recast(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    body = {"peer": args.peer, "new_peer": args.new_peer, "family": args.family,
            "reason": args.reason}
    if args.extend_seconds is not None:
        body["extend_seconds"] = args.extend_seconds
    status, payload = post_json(f"{base}/committee/{_quote(args.channel)}/recast",
                                body, token)
    if not _ok(status):
        return _fail("recast", status, payload)
    print(f"recast {payload.get('voided_peer')} -> {payload.get('seated')}")
    return 0


def _run_baseline_absent(args: argparse.Namespace) -> int:
    _, token = _auth(args)
    base = args.gateway.rstrip("/")
    motion = _open_motion(base, token, args.channel, "baseline-absent")
    body = {"reason": args.reason}
    status, payload = post_json(
        f"{base}/committee/{_quote(args.channel)}/motions/{motion['id']}/baseline-absent",
        body, token)
    if not _ok(status):
        return _fail("baseline-absent", status, payload)
    print(f"recorded baseline ABSENT on motion {payload.get('motion_id')}")
    return 0


_DISPATCH = {
    "motion": _run_motion,
    "post": _run_post,
    "results": _run_results,
    "rooms": _run_rooms,
    "recast": _run_recast,
    "baseline-absent": _run_baseline_absent,
}


def run_committee(argv: list) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return _DISPATCH[args.command](args)
    except RuntimeError as exc:
        print(f"swarph committee: {exc}", file=sys.stderr)
        return 2
