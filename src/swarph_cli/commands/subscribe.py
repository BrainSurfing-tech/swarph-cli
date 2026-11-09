"""Targeted, expiring event subscriptions (card #1523)."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from ._display import sanitize_terminal as _s
from ._gateway_client import add_common_args, get_json, post_json, resolve_self_name, resolve_token


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarph subscribe", description=__doc__)
    commands = parser.add_subparsers(dest="command")
    add = commands.add_parser("add", help="subscribe one cell to an event")
    add.add_argument("--trigger", required=True, choices=("obligation_state", "card_stage", "time"))
    add.add_argument("--target", required=True, help="obligation/card id, or daily HH:MMZ")
    add.add_argument("--cell", help="recipient cell (default: yourself; only lab/commander may name another)")
    expiry = add.add_mutually_exclusive_group()
    expiry.add_argument("--days", type=int, help="expiry in days (gateway default: 7, maximum: 30)")
    expiry.add_argument("--expires-at", help="expiry as a UTC ISO timestamp")
    add.add_argument("--json", action="store_true")
    add_common_args(add)
    listing = commands.add_parser("list", help="list your subscriptions")
    listing.add_argument("--cell", help="recipient cell (default: yourself)")
    listing.add_argument("--json", action="store_true")
    add_common_args(listing)
    cancel = commands.add_parser("cancel", help="cancel one subscription")
    cancel.add_argument("id", type=int, help="subscription id")
    cancel.add_argument("--json", action="store_true")
    add_common_args(cancel)
    return parser


def run_subscribe(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        actor = resolve_self_name(args.self_name)
        token = resolve_token(actor, args.token_file)
        url = f"{args.gateway.rstrip('/')}/subscriptions"
        if args.command == "add":
            body = {
                "actor": actor, "cell": args.cell if args.cell is not None else actor,
                "trigger": args.trigger, "target": args.target,
            }
            if args.days is not None:
                body["days"] = args.days
            if args.expires_at is not None:
                body["expires_at"] = args.expires_at
            status, payload = post_json(url, body, token)
            line = f"created subscription #{_s(payload.get('id'))}"
        elif args.command == "list":
            query = {"actor": actor}
            if args.cell is not None:
                query["cell"] = args.cell
            status, payload = get_json(f"{url}?{urllib.parse.urlencode(query)}", token)
            line = "\n".join(f"#{_s(row.get('id'))}: {_s(row.get('trigger'))} {_s(row.get('target'))} "
                             f"-> {_s(row.get('cell'))} until {_s(row.get('expires_at'))}"
                             + (f" cancelled={_s(row['cancelled_at'])}" if row.get("cancelled_at") else "")
                             for row in payload.get("subscriptions", [])) or "no subscriptions"
        else:
            status, payload = post_json(f"{url}/{args.id}/cancel", {"actor": actor}, token)
            line = f"cancelled subscription #{args.id}"
    except RuntimeError as exc:
        print(f"swarph subscribe: {_s(exc)}", file=sys.stderr)
        return 1
    if not 200 <= status < 300:
        print(f"swarph subscribe: gateway {status or 'unreachable'}: {_s(payload.get('detail', payload))}",
              file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2) if args.json else line)
    return 0
