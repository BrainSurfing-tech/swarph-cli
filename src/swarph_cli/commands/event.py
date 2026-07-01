"""``swarph event`` — emit an event to a mesh channel (event-chaining P0).

``swarph event emit <event> <payload> [--chain-token <opaque>] [--channel events]``
posts to a channel via the shipped channel-post client, carrying an ``event`` tag,
the payload, and any ``--chain-token`` as an OPAQUE passthrough. The cell never
signs or parses the chain-token — signing/verifying is the producer's authority
(the P1 gateway guard). See ``swarph_cli.chain_token`` for the producer-side
sign/verify helpers.
"""

from __future__ import annotations

import argparse
import json
import sys

from ._gateway_client import (
    add_common_args,
    post_json,
    resolve_self_name,
    resolve_token,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarph event",
        description="Emit an event to a mesh channel (opaque chain-token passthrough).",
    )
    sub = p.add_subparsers(dest="command")

    emit = sub.add_parser("emit", help="emit an event to a channel")
    emit.add_argument("event", help="event name/tag")
    emit.add_argument("payload", help="event payload (opaque string)")
    emit.add_argument(
        "--chain-token",
        default=None,
        help="opaque producer-signed chain token — passed through, never parsed",
    )
    emit.add_argument(
        "--channel",
        default="events",
        help="target channel (default: events)",
    )
    add_common_args(emit)
    return p


def _post_channel(
    *,
    channel: str,
    event: str,
    payload: str,
    chain_token: str | None = None,
    gateway: str | None = None,
    self_name: str | None = None,
    token_file: str | None = None,
) -> int:
    """Bridge to the shipped channel-post client (the ``/messages`` channel path
    that ``swarph channel`` / ``swarph mesh`` post through). Wraps the event tag +
    payload + (opaque) chain-token into the post body and returns 0 on a 2xx.

    NOTE: the plan's seam is ``_post_channel(**kw)``; the gateway's ``post_json``
    takes ``(url, body, token)``, so this function resolves identity/token and
    shapes the body, then delegates to that same shared client — it does NOT
    reinvent the gateway post path. Tests monkeypatch this function directly.
    """
    name = resolve_self_name(self_name)
    token = resolve_token(name, token_file)
    from .mesh import _DEFAULT_GATEWAY

    base = (gateway or _DEFAULT_GATEWAY).rstrip("/")
    content: dict = {"event": event, "payload": payload}
    if chain_token:
        content["chain_token"] = chain_token
    body = {
        "from_node": name,
        "channel": channel,
        "kind": "fyi",
        "content": json.dumps(content, separators=(",", ":"), sort_keys=True),
    }
    status, resp = post_json(f"{base}/messages", body, token)
    if 200 <= status < 300:
        print(f"emitted event={event} channel={channel} id={resp.get('id')}")
        return 0
    detail = resp.get("detail", "<gateway error>")
    print(f"swarph event emit: gateway {status}: {detail}", file=sys.stderr)
    return 1


def run_event(argv: list) -> int:
    p = _build_parser()
    args = p.parse_args(argv)
    if args.command is None:
        p.print_help()
        return 0
    try:
        if args.command == "emit":
            return _post_channel(
                channel=args.channel,
                event=args.event,
                payload=args.payload,
                chain_token=args.chain_token,
                gateway=args.gateway,
                self_name=args.self_name,
                token_file=args.token_file,
            )
    except RuntimeError as e:
        print(f"swarph event: {e}", file=sys.stderr)
        return 2
    p.print_help()
    return 0
