"""``swarph schedule`` — client for the mesh-gateway scheduled-event endpoints.

These endpoints are operator-gated server-side; this client only shapes and
sends the requests. A 403 (or any non-2xx) is surfaced verbatim from the
gateway, so an unauthorized caller sees the operator-only refusal.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from ._gateway_client import (
    add_common_args,
    delete_json,
    get_json,
    post_json,
    resolve_self_name,
    resolve_token,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph schedule",
        description="Manage mesh-gateway scheduled events.",
    )
    sub = p.add_subparsers(dest="command")

    create = sub.add_parser("create", help="create a scheduled event")
    create.add_argument("name", help="event name")
    create.add_argument("--trigger", required=True, help="trigger type: time|event")
    create.add_argument("--target", required=True, help="target cell")
    create.add_argument("--task", required=True, help="task text")
    create.add_argument("--cron", default=None, help="cron expression (time trigger)")
    create.add_argument("--out-channel", default=None, help="output channel")
    create.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="durable context anchor, repeatable. KEY is one of "
             + "|".join(sorted(DURABLE_ANCHOR_KEYS))
             + " (e.g. --context memory=project_x --context repo=swarph-cli). "
             "A raw JSON object is also accepted. REQUIRED: the gateway rejects an "
             "event with no anchors, because an event that fires with no durable "
             "context wakes a cell that cannot reconstruct why.",
    )
    create.add_argument(
        "--min-interval",
        type=int,
        default=None,
        help="minimum seconds between firings",
    )
    add_common_args(create)

    ls = sub.add_parser("list", help="list scheduled events")
    add_common_args(ls)

    get = sub.add_parser("get", help="show one scheduled event")
    get.add_argument("name", help="event name")
    add_common_args(get)

    enable = sub.add_parser("enable", help="enable a scheduled event")
    enable.add_argument("name", help="event name")
    add_common_args(enable)

    disable = sub.add_parser("disable", help="disable a scheduled event")
    disable.add_argument("name", help="event name")
    add_common_args(disable)

    delete = sub.add_parser("delete", help="delete a scheduled event")
    delete.add_argument("name", help="event name")
    add_common_args(delete)

    fire = sub.add_parser("fire-now", help="fire a scheduled event immediately")
    fire.add_argument("name", help="event name")
    add_common_args(fire)

    return p


def _fail(sub: str, status: int, payload: dict) -> int:
    detail = payload.get("detail", "<error>")
    print(f"swarph schedule {sub}: gateway {status}: {detail}", file=sys.stderr)
    return 1


def _ok(status: int) -> bool:
    return 200 <= status < 300


def _ctx(args: argparse.Namespace) -> tuple[str, str, str]:
    self_name = resolve_self_name(args.self_name)
    token = resolve_token(self_name, args.token_file)
    base = args.gateway.rstrip("/")
    return self_name, token, base



# Mirrors mesh-gateway `_DURABLE_ANCHOR_KEYS` (server.py). Kept in sync by
# test_schedule_create_contract.py, which fails if this drifts from the value the
# gateway actually enforces — a duplicated constant nobody checks is how the two
# sides diverged in the first place.
DURABLE_ANCHOR_KEYS = frozenset({"repo", "memory", "channel", "feature", "file"})


def parse_context_anchor(raw: str) -> dict:
    """Turn one --context value into the ANCHOR DICT the gateway requires.

    >>> THE BUG THIS FIXES (card #146): the CLI sent `--context` values through as
        RAW STRINGS while the gateway requires each anchor to be a DICT carrying a
        durable key, and requires the list to be NON-EMPTY. So `swarph schedule
        create` COULD NOT SUCCEED — every invocation 400'd, and every real event on
        this mesh had to be armed by hand over the raw API. <<<

    That is why dated reminders kept living in DM threads instead of the scheduler:
    the verb that makes a date fire was itself broken, so the cheapest path was
    always to tell a peer and hope.

    Accepts `key=value` for the five durable kinds, or a raw JSON object for
    anything the shorthand cannot express. Raises ValueError with the valid keys
    listed, LOCALLY — a 400 from the gateway names the rule but not the input that
    broke it, and the user is holding the input.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty --context value")
    if raw.startswith("{"):
        try:
            anchor = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--context {raw!r} looks like JSON but does not parse: {exc}") from exc
        if not isinstance(anchor, dict):
            raise ValueError(f"--context JSON must be an object, got {type(anchor).__name__}")
        if not (set(anchor) & DURABLE_ANCHOR_KEYS):
            raise ValueError(
                f"--context {raw!r} has no durable key; one of "
                f"{sorted(DURABLE_ANCHOR_KEYS)} is required")
        return anchor
    key, sep, value = raw.partition("=")
    key, value = key.strip(), value.strip()
    if not sep or not value:
        raise ValueError(
            f"--context {raw!r} must be KEY=VALUE (e.g. memory=project_x); "
            f"KEY is one of {sorted(DURABLE_ANCHOR_KEYS)}")
    if key not in DURABLE_ANCHOR_KEYS:
        raise ValueError(
            f"--context key {key!r} is not durable; use one of {sorted(DURABLE_ANCHOR_KEYS)}. "
            f"An anchor must name something that survives compaction — a /tmp path or a "
            f"session id is exactly what the gateway rejects.")
    return {key: value}


def _run_create(args: argparse.Namespace) -> int:
    self_name, token, base = _ctx(args)
    if not args.context:
        print("swarph schedule create: --context is REQUIRED (repeatable).\n"
              "  The gateway refuses an event with no durable anchor: an event that\n"
              "  fires with no context wakes a cell that cannot reconstruct why it woke.\n"
              f"  Use KEY=VALUE with KEY in {sorted(DURABLE_ANCHOR_KEYS)},\n"
              "  e.g. --context memory=project_graduation_register", file=sys.stderr)
        return 2
    try:
        anchors = [parse_context_anchor(c) for c in args.context]
    except ValueError as exc:
        print(f"swarph schedule create: {exc}", file=sys.stderr)
        return 2
    body = {
        "name": args.name,
        "trigger_type": args.trigger,
        "target_cell": args.target,
        "task": args.task,
        "context_ref": anchors,
        "created_by": self_name,
    }
    if args.cron is not None:
        body["cron"] = args.cron
    if args.out_channel is not None:
        body["out_channel"] = args.out_channel
    if args.min_interval is not None:
        body["min_interval_sec"] = args.min_interval
    status, payload = post_json(f"{base}/scheduled-events", body, token)
    if not _ok(status):
        return _fail("create", status, payload)
    print(f"scheduled {args.name}")
    return 0


def _run_list(args: argparse.Namespace) -> int:
    _self, token, base = _ctx(args)
    status, payload = get_json(f"{base}/scheduled-events", token)
    if not _ok(status):
        return _fail("list", status, payload)
    print(json.dumps(payload, indent=2))
    return 0


def _run_get(args: argparse.Namespace) -> int:
    _self, token, base = _ctx(args)
    name = urllib.parse.quote(args.name, safe="")
    status, payload = get_json(f"{base}/scheduled-events/{name}", token)
    if not _ok(status):
        return _fail("get", status, payload)
    print(json.dumps(payload, indent=2))
    return 0


def _run_action(args: argparse.Namespace, sub: str, action: str, verb: str) -> int:
    _self, token, base = _ctx(args)
    name = urllib.parse.quote(args.name, safe="")
    status, payload = post_json(
        f"{base}/scheduled-events/{name}/{action}", {}, token
    )
    if not _ok(status):
        return _fail(sub, status, payload)
    print(f"{verb} {args.name}")
    return 0


def _run_delete(args: argparse.Namespace) -> int:
    _self, token, base = _ctx(args)
    name = urllib.parse.quote(args.name, safe="")
    status, payload = delete_json(f"{base}/scheduled-events/{name}", token)
    if not _ok(status):
        return _fail("delete", status, payload)
    print(f"deleted {args.name}")
    return 0


def run_schedule(argv: list) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "create":
            return _run_create(args)
        if args.command == "list":
            return _run_list(args)
        if args.command == "get":
            return _run_get(args)
        if args.command == "enable":
            return _run_action(args, "enable", "enable", "enabled")
        if args.command == "disable":
            return _run_action(args, "disable", "disable", "disabled")
        if args.command == "delete":
            return _run_delete(args)
        if args.command == "fire-now":
            return _run_action(args, "fire-now", "fire-now", "fired")
        parser.error(f"unknown command: {args.command}")
    except RuntimeError as exc:
        print(f"swarph schedule: {exc}", file=sys.stderr)
        return 2
    return 2
